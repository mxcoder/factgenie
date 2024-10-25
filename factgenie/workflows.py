#!/usr/bin/env python3
import os
import datetime
import json
import time
import logging
import pandas as pd
import time
import traceback
import yaml
import shutil
import importlib
import zipfile
import traceback
import tempfile

import factgenie.utils as utils

from io import BytesIO
from slugify import slugify
from flask import make_response
from collections import defaultdict
from pathlib import Path
from factgenie.campaigns import (
    HumanCampaign,
    LLMCampaignEval,
    ExternalCampaign,
    LLMCampaignGen,
    CampaignMode,
    CampaignStatus,
)

from factgenie import (
    CAMPAIGN_DIR,
    OUTPUT_DIR,
    INPUT_DIR,
    LLM_EVAL_CONFIG_DIR,
    LLM_GEN_CONFIG_DIR,
    CROWDSOURCING_CONFIG_DIR,
)

file_handler = logging.FileHandler("error.log")
file_handler.setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def get_dataset(app, dataset_id):
    return app.db["datasets_obj"].get(dataset_id)


def load_configs(mode):
    """
    Goes through all the files in the LLM_CONFIG_DIR
    instantiate the LLMMetric class
    and inserts the object in the metrics dictionary

    Returns:
        metrics: dictionary of LLMMetric objects with keys of metric names
    """
    configs = {}

    if mode == CampaignMode.LLM_EVAL:
        config_dir = LLM_EVAL_CONFIG_DIR
    elif mode == CampaignMode.LLM_GEN:
        config_dir = LLM_GEN_CONFIG_DIR
    elif mode == CampaignMode.CROWDSOURCING:
        config_dir = CROWDSOURCING_CONFIG_DIR

    for file in os.listdir(config_dir):
        if file.endswith(".yaml"):
            try:
                with open(config_dir / file) as f:
                    config = yaml.safe_load(f)
                    configs[file] = config
            except Exception as e:
                logger.error(f"Error while loading metric {file}")
                traceback.print_exc()
                continue

    return configs


def get_example_data(app, dataset_id, split, example_idx, setup_id=None):
    dataset = get_dataset(app=app, dataset_id=dataset_id)

    try:
        example = dataset.get_example(split=split, example_idx=example_idx)
    except:
        raise ValueError("Example cannot be retrieved from the dataset")

    try:
        html = dataset.render(example=example)
    except:
        raise ValueError("Example cannot be rendered")

    # temporary solution for external files
    # prefix all the "/files" calls with "app.config["host_prefix"]"
    html = html.replace('src="/files', f'src="{app.config["host_prefix"]}/files')

    if setup_id:
        generated_outputs = [get_output_for_setup(dataset_id, split, example_idx, setup_id)]
    else:
        generated_outputs = get_outputs(app, dataset_id, split, example_idx)

    for i, output in enumerate(generated_outputs):
        setup_id = output["setup_id"]
        annotations = get_annotations(app, dataset_id, split, example_idx, setup_id)

        generated_outputs[i]["annotations"] = annotations

    return {
        "html": html,
        "raw_data": example,
        "generated_outputs": generated_outputs,
    }


def instantiate_campaign(app, campaign_id, mode):
    campaign = None

    if mode == CampaignMode.CROWDSOURCING:
        scheduler = app.db["scheduler"]
        campaign = HumanCampaign(campaign_id=campaign_id, scheduler=scheduler)
    elif mode == CampaignMode.LLM_EVAL:
        campaign = LLMCampaignEval(campaign_id=campaign_id)
    elif mode == CampaignMode.LLM_GEN:
        campaign = LLMCampaignGen(campaign_id=campaign_id)
    elif mode == CampaignMode.EXTERNAL:
        campaign = ExternalCampaign(campaign_id=campaign_id)
    elif mode == CampaignMode.HIDDEN:
        pass
    else:
        logger.warning(f"Unknown campaign mode: {mode}")

    return campaign


def load_campaign(app, campaign_id):
    campaign_index = generate_campaign_index(app, force_reload=False)

    if campaign_id not in campaign_index:
        logger.error(f"Unknown campaign {campaign_id}")
        return None

    campaign = campaign_index[campaign_id]
    return campaign


def generate_campaign_index(app, force_reload=True):
    if "campaign_index" in app.db:
        campaign_index = app.db["campaign_index"]
    else:
        campaign_index = defaultdict(dict)

    existing_campaign_ids = set()
    for campaign_dir in Path(CAMPAIGN_DIR).iterdir():
        try:
            metadata = json.load(open(campaign_dir / "metadata.json"))
            mode = metadata["mode"]
            campaign_id = metadata["id"]
            existing_campaign_ids.add(campaign_id)

            if campaign_id in campaign_index and not force_reload:
                continue

            campaign = instantiate_campaign(app=app, campaign_id=campaign_id, mode=mode)

            if (
                (mode == CampaignMode.LLM_EVAL or mode == CampaignMode.LLM_GEN)
                and campaign.metadata["status"] == CampaignStatus.RUNNING
                and campaign_id not in app.db["running_campaigns"]
            ):
                campaign.metadata["status"] = CampaignStatus.IDLE
                campaign.update_metadata()

            campaign_index[campaign_id] = campaign

        except:
            traceback.print_exc()
            logger.error(f"Error while loading campaign {campaign_dir}")

    # remove campaigns that are no longer in the directory
    campaign_index = {k: v for k, v in campaign_index.items() if k in existing_campaign_ids}

    app.db["campaign_index"] = campaign_index

    return app.db["campaign_index"]


def load_annotations_for_campaign(subdir):
    annotations_campaign = []

    # find metadata for the campaign
    metadata_path = CAMPAIGN_DIR / subdir / "metadata.json"
    if not metadata_path.exists():
        return []

    with open(metadata_path) as f:
        metadata = json.load(f)

    if metadata["mode"] == CampaignMode.HIDDEN or metadata["mode"] == CampaignMode.LLM_GEN:
        return []

    jsonl_files = (CAMPAIGN_DIR / subdir / "files").glob("*.jsonl")

    for jsonl_file in jsonl_files:
        with open(jsonl_file) as f:
            for line in f:
                annotation_records = load_annotations_from_record(line, metadata)
                annotations_campaign.append(annotation_records[0])

    return annotations_campaign


def create_annotation_example_record(j, metadata):
    return {
        "annotation_span_categories": metadata["config"]["annotation_span_categories"],
        "annotator_id": j["annotator_id"],
        "annotator_group": j.get("annotator_group", 0),
        "campaign_id": slugify(metadata["id"]),
        "dataset": slugify(j["dataset"]),
        "example_idx": int(j["example_idx"]),
        "setup_id": slugify(j["setup_id"]),
        "split": slugify(j["split"]),
        "flags": j.get("flags", []),
        "options": j.get("options", []),
        "text_fields": j.get("text_fields", []),
    }


def load_annotations_from_record(line, metadata, split_spans=False):
    j = json.loads(line)
    annotation_records = []

    r = create_annotation_example_record(j, metadata)

    if split_spans:
        for annotation in j["annotations"]:
            r["annotation_type"] = int(annotation["type"])
            r["annotation_start"] = annotation["start"]
            r["annotation_text"] = annotation["text"]

            annotation_records.append(r.copy())
    else:
        r["annotations"] = j["annotations"]
        annotation_records.append(r)

    return annotation_records


def get_annotation_index(app, force_reload=True):
    if app and app.db["annotation_index"] is not None and not force_reload:
        return app.db["annotation_index"]

    # contains annotations for each generated output
    annotations = []

    # for all subdirectories in CAMPAIGN_DIR, load content of all the jsonl files
    for subdir in os.listdir(CAMPAIGN_DIR):
        try:
            annotations += load_annotations_for_campaign(subdir)
        except:
            traceback.print_exc()
            logger.error(f"Error while loading annotations for {subdir}")

    annotation_index = pd.DataFrame.from_records(annotations)
    app.db["annotation_index"] = annotation_index

    return annotation_index


def get_annotations(app, dataset_id, split, example_idx, setup_id):
    annotation_index = get_annotation_index(app, force_reload=False)

    if annotation_index.empty:
        return []

    annotations = annotation_index[
        (annotation_index["dataset"] == dataset_id)
        & (annotation_index["split"] == split)
        & (annotation_index["example_idx"] == example_idx)
        & (annotation_index["setup_id"] == setup_id)
    ]

    return annotations.to_dict(orient="records")


def get_output_index(app=None, force_reload=True):
    if app and app.db["output_index"] is not None and not force_reload:
        return app.db["output_index"]

    outputs = []

    # find recursively all JSONL files in the output directory
    outs = list(Path(OUTPUT_DIR).rglob("*.jsonl"))

    for out in outs:
        with open(out) as f:
            for line_num, line in enumerate(f):
                try:
                    j = json.loads(line)

                    for key in ["dataset", "split", "setup_id"]:
                        j[key] = slugify(j[key])

                    # drop any keys that are not in the key set
                    j = {
                        k: v
                        for k, v in j.items()
                        if k in ["dataset", "split", "setup_id", "example_idx", "out"]
                    }

                    outputs.append(j)
                except Exception as e:
                    logger.error(
                        f"Error parsing output file {out} at line {line_num + 1}:\n\t{e.__class__.__name__}: {e}"
                    )

    output_index = pd.DataFrame.from_records(outputs)

    if app:
        app.db["output_index"] = output_index

    return output_index


def export_campaign_outputs(campaign_id):
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        for root, _dirs, files in os.walk(os.path.join(CAMPAIGN_DIR, campaign_id)):
            for file in files:
                zip_file.write(
                    os.path.join(root, file),
                    os.path.relpath(os.path.join(root, file), os.path.join(CAMPAIGN_DIR, campaign_id)),
                )

    # Set response headers for download
    timestamp = int(time.time())
    response = make_response(zip_buffer.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename={campaign_id}_{timestamp}.zip"
    return response


def get_local_dataset_overview(app):
    config = utils.load_dataset_config()
    overview = {}

    for dataset_id, dataset_config in config.items():
        class_name = dataset_config["class"]
        params = dataset_config.get("params", {})
        is_enabled = dataset_config.get("enabled", True)
        name = dataset_config.get("name", dataset_id)
        description = dataset_config.get("description", "")
        splits = dataset_config.get("splits", [])
        dataset_type = dataset_config.get("type", "default")

        if is_enabled:
            dataset = app.db["datasets_obj"].get(dataset_id)

            if dataset is None:
                logger.warning(f"Dataset {dataset_id} is enabled but not loaded, loading...")
                try:
                    dataset = instantiate_dataset(dataset_id, dataset_config)
                    app.db["datasets_obj"][dataset_id] = dataset
                except Exception as e:
                    logger.error(f"Error while loading dataset {dataset_id}")
                    traceback.print_exc()
                    continue

            example_count = {split: dataset.get_example_count(split) for split in dataset.get_splits()}
        else:
            example_count = {}

        overview[dataset_id] = {
            "class": class_name,
            "params": params,
            "enabled": is_enabled,
            "splits": splits,
            "name": name,
            "description": description,
            "example_count": example_count,
            "type": dataset_type,
        }

    return overview


def get_dataset_class(submodule, class_name):
    # Dynamically import the class
    module = importlib.import_module("factgenie.datasets")
    submodule = getattr(module, submodule)
    dataset_class = getattr(submodule, class_name)

    return dataset_class


def get_resources(app):
    config = utils.load_resources_config()

    return config


def download_dataset(app, dataset_id):
    config = utils.load_resources_config()
    dataset_config = config.get(dataset_id)

    if dataset_config is None:
        raise ValueError(f"Dataset {dataset_id} not found in the download config")

    submodule, class_name = dataset_config["class"].split(".")

    dataset_cls = get_dataset_class(submodule, class_name)
    download_dir = INPUT_DIR / dataset_id
    output_dir = OUTPUT_DIR
    campaign_dir = CAMPAIGN_DIR

    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    dataset_cls.download(
        dataset_id=dataset_id,
        data_download_dir=download_dir,
        out_download_dir=output_dir,
        annotation_download_dir=campaign_dir,
        splits=dataset_config["splits"],
        outputs=dataset_config.get("outputs", []),
        dataset_config=dataset_config,
    )

    # add an entry in the dataset config
    config = utils.load_dataset_config()

    config[dataset_id] = {
        "class": dataset_config["class"],
        "name": dataset_config.get("name", dataset_id),
        "description": dataset_config.get("description", ""),
        "splits": dataset_config["splits"],
        "enabled": True,
    }

    dataset = instantiate_dataset(dataset_id, config[dataset_id])
    app.db["datasets_obj"][dataset_id] = dataset

    utils.save_dataset_config(config)

    return dataset


def delete_dataset(app, dataset_id):
    config = utils.load_dataset_config()
    config.pop(dataset_id, None)
    utils.save_dataset_config(config)

    # remove the data directory
    shutil.rmtree(INPUT_DIR / dataset_id, ignore_errors=True)

    delete_model_outputs(dataset_id, None, None)

    app.db["datasets_obj"].pop(dataset_id, None)


def export_dataset(app, dataset_id):
    zip_buffer = BytesIO()
    data_path = INPUT_DIR / dataset_id

    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        for root, dirs, files in os.walk(data_path):
            for file in files:
                zip_file.write(
                    os.path.join(root, file),
                    os.path.relpath(os.path.join(root, file), data_path),
                )

    # Set response headers for download
    response = make_response(zip_buffer.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename={dataset_id}.zip"

    return response


def instantiate_dataset(dataset_id, dataset_config):
    submodule, class_name = dataset_config["class"].split(".")

    dataset_class = get_dataset_class(submodule, class_name)

    return dataset_class(dataset_id, **dataset_config)


def instantiate_datasets():
    config = utils.load_dataset_config()
    datasets = {}

    for dataset_id, dataset_config in config.items():
        is_enabled = dataset_config.get("enabled", True)

        if not is_enabled:
            continue

        try:
            datasets[dataset_id] = instantiate_dataset(dataset_id, dataset_config)
        except Exception as e:
            logger.error(f"Error while loading dataset {dataset_id}")
            traceback.print_exc()

    return datasets


def set_dataset_enabled(app, dataset_id, enabled):
    config = utils.load_dataset_config()
    config[dataset_id]["enabled"] = enabled

    if enabled:
        dataset = instantiate_dataset(dataset_id, config[dataset_id])
        app.db["datasets_obj"][dataset_id] = dataset
    else:
        app.db["datasets_obj"].pop(dataset_id, None)

    utils.save_dataset_config(config)


def upload_dataset(app, dataset_id, dataset_name, dataset_description, dataset_format, dataset_data):
    params = {
        "text": {"suffix": "txt", "class": "basic.PlainTextDataset", "type": "default"},
        "jsonl": {"suffix": "jsonl", "class": "basic.JSONLDataset", "type": "json"},
        "csv": {"suffix": "csv", "class": "basic.CSVDataset", "type": "table"},
        "html": {"suffix": "zip", "class": "basic.HTMLDataset", "type": "default"},
    }
    data_dir = INPUT_DIR / dataset_id
    os.makedirs(data_dir, exist_ok=True)

    # slugify all split names
    dataset_data = {slugify(k): v for k, v in dataset_data.items()}

    if dataset_format in ["text", "jsonl", "csv"]:
        # save each split in a separate file
        for split, data in dataset_data.items():
            with open(f"{data_dir}/{split}.{params[dataset_format]['suffix']}", "w") as f:
                f.write(data)

    elif dataset_format == "html":
        # dataset_data is the file object
        for split, data in dataset_data.items():
            binary_file = BytesIO(bytes(data))

            # check if there is at least one HTML file in the top-level directory
            with zipfile.ZipFile(binary_file, "r") as zip_ref:
                for file in zip_ref.namelist():
                    if file.endswith(".html") and "/" not in file:
                        break
                else:
                    raise ValueError("No HTML files found in the zip archive")

                zip_ref.extractall(f"{data_dir}/{split}")

    # add an entry in the dataset config
    config = utils.load_dataset_config()
    if dataset_id in config:
        if config[dataset_id]["class"] != params[dataset_format]["class"]:
            raise ValueError(f"Dataset {dataset_id} already exists with a different class")

        elif any([split in config[dataset_id]["splits"] for split in dataset_data.keys()]):
            raise ValueError(f"Dataset {dataset_id} already exists with the same split")
        else:
            # if the user uploads a new split, add it to the existing dataset
            config[dataset_id]["splits"] = list(set(config[dataset_id]["splits"] + list(dataset_data.keys())))

            # update description:
            config[dataset_id]["description"] = dataset_description
    else:
        config[dataset_id] = {
            "name": dataset_name,
            "class": params[dataset_format]["class"],
            "description": dataset_description,
            "splits": list(dataset_data.keys()),
            "enabled": True,
        }
    utils.save_dataset_config(config)

    app.db["datasets_obj"][dataset_id] = instantiate_dataset(dataset_id, config[dataset_id])


def delete_model_outputs(dataset, split=None, setup_id=None):
    path = Path(OUTPUT_DIR)

    # look through all JSON files in the output directory
    for file in path.rglob("*.jsonl"):
        new_lines = []

        with open(file) as f:
            for line in f:
                j = json.loads(line)

                # None means all
                if (split is None or j["split"] == split) and (setup_id is None or j["setup_id"] == setup_id):
                    continue

                new_lines.append(line)

        if len(new_lines) == 0:
            os.remove(file)
        else:
            with open(file, "w") as f:
                f.writelines(new_lines)


def export_outputs(app, dataset_id, split, setup_id):
    zip_buffer = BytesIO()

    # assemble relevant outputs
    output_index = get_output_index(app)

    if output_index.empty:
        raise ValueError("No outputs found")

    outputs = output_index[
        (output_index["dataset"] == dataset_id)
        & (output_index["split"] == split)
        & (output_index["setup_id"] == setup_id)
    ]
    # write the outputs to a temporary JSONL file
    tmp_file_path = tempfile.mktemp()

    with open(tmp_file_path, "w") as f:
        for _, row in outputs.iterrows():
            j = row.to_dict()
            f.write(json.dumps(j) + "\n")

    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        zip_file.write(
            tmp_file_path,
            f"{dataset_id}-{split}-{setup_id}.jsonl",
        )

    # Set response headers for download
    response = make_response(zip_buffer.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename={dataset_id}_{split}_{setup_id}.zip"

    return response


def get_model_outputs_overview(app, datasets, non_empty=False):
    output_index = get_output_index(app)

    if output_index.empty:
        return []

    # filter the df by datasets
    outputs = output_index.copy()
    outputs = outputs[outputs["dataset"].isin(datasets)]

    # if non_empty, filter only the examples with outputs
    if non_empty:
        outputs = outputs[outputs["out"].notnull()]

    # aggregate `example_idx` to list, drop "in", "out", "metadata"
    outputs = (
        outputs.groupby(["dataset", "split", "setup_id"])
        .agg(example_idx=pd.NamedAgg(column="example_idx", aggfunc=list))
        .reset_index()
    )
    # rename "example_idx" to "output_ids"
    outputs = outputs.rename(columns={"example_idx": "output_ids"})
    outputs = outputs.to_dict(orient="records")

    return outputs


def get_output_for_setup(dataset, split, example_idx, setup_id):
    output_index = get_output_index()

    if output_index.empty:
        return None

    output = output_index[
        (output_index["dataset"] == dataset)
        & (output_index["split"] == split)
        & (output_index["setup_id"] == setup_id)
        & (output_index["example_idx"] == example_idx)
    ]

    if len(output) == 0:
        return None

    return output.to_dict(orient="records")[0]


def get_outputs(app, dataset_id, split, example_idx):
    outputs = get_output_index(app, force_reload=False)

    if outputs.empty:
        return []

    outputs = outputs[
        (outputs["dataset"] == dataset_id) & (outputs["split"] == split) & (outputs["example_idx"] == example_idx)
    ]

    outputs = outputs.to_dict(orient="records")

    return outputs


def get_output_ids(dataset, split, setup_id):
    output_index = get_output_index()

    if output_index.empty:
        return []

    output_ids = output_index[
        (output_index["dataset"] == dataset) & (output_index["split"] == split) & (output_index["setup_id"] == setup_id)
    ]["example_idx"].tolist()

    return output_ids


def upload_model_outputs(dataset, split, setup_id, model_outputs):
    path = Path(OUTPUT_DIR) / dataset.id
    path.mkdir(parents=True, exist_ok=True)

    generated = model_outputs.strip().split("\n")
    setup_id = slugify(setup_id)

    if len(generated) != len(dataset.examples[split]):
        raise ValueError(
            f"Output count mismatch for {setup_id} in {split}: {len(generated)} vs {len(dataset.examples[split])}"
        )

    with open(f"{path}/{split}-{setup_id}.jsonl", "w") as f:
        for i, out in enumerate(generated):
            j = {
                "dataset": dataset.id,
                "split": split,
                "setup_id": setup_id,
                "example_idx": i,
                "out": out,
            }
            f.write(json.dumps(j) + "\n")

    with open(f"{path.parent}/metadata.json", "w") as f:
        json.dump(
            {
                "id": setup_id,
                "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            indent=4,
        )


def get_sorted_campaign_list(app, modes):
    campaign_index = generate_campaign_index(app, force_reload=True)

    campaigns = [c for c in campaign_index.values() if c.metadata["mode"] in modes]

    campaigns.sort(key=lambda x: x.metadata["created"], reverse=True)
    campaigns = {
        c.metadata["id"]: {"metadata": c.metadata, "stats": c.get_stats(), "data": c.db.to_dict(orient="records")}
        for c in campaigns
    }
    return campaigns


def generate_default_id(app, mode, prefix):
    campaign_list = get_sorted_campaign_list(app, modes=[mode])

    i = 1
    default_campaign_id = f"{prefix}-{i}"
    while default_campaign_id in campaign_list.keys():
        default_campaign_id = f"{prefix}-{i}"
        i += 1

    return default_campaign_id
