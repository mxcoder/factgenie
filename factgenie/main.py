#!/usr/bin/env python3
import os
import json
import time
import logging
import time
import threading
import traceback
import shutil
import datetime
import markdown
import urllib.parse

import factgenie.workflows as workflows
import factgenie.analysis as analysis
import factgenie.utils as utils

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    Response,
    make_response,
    redirect,
    send_from_directory,
)
from collections import defaultdict
from slugify import slugify

from factgenie import PREVIEW_STUDY_ID, CAMPAIGN_DIR, TEMPLATES_DIR, STATIC_DIR
from factgenie.campaigns import CampaignMode, CampaignStatus, ExampleStatus
from factgenie.models import ModelFactory
from factgenie.datasets.dataset import get_dataset_classes

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask("factgenie", template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.db = {}
app.db["annotation_index"] = {}
app.db["lock"] = threading.Lock()
app.db["threads"] = {}
app.db["announcers"] = {}
app.wsgi_app = ProxyFix(app.wsgi_app, x_host=1)

logger = logging.getLogger(__name__)


# -----------------
# Jinja filters
# -----------------
@app.template_filter("ctime")
def timectime(timestamp):
    try:
        s = datetime.datetime.fromtimestamp(timestamp)
        return s.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return timestamp


@app.template_filter("elapsed")
def time_elapsed(batch):
    start_timestamp = batch["start"]
    end_timestamp = batch["end"]
    try:
        if end_timestamp:
            s = datetime.datetime.fromtimestamp(start_timestamp)
            e = datetime.datetime.fromtimestamp(end_timestamp)
            diff = str(e - s)
            return diff.split(".")[0]
        else:

            s = datetime.datetime.fromtimestamp(start_timestamp)
            diff = str(datetime.datetime.now() - s)
            return diff.split(".")[0]
    except:
        return ""


@app.template_filter("annotate_url")
def annotate_url(current_url):
    # get the base url (without any "browse", "crowdsourcing" or "crowdsourcing/campaign" in it)
    parsed = urllib.parse.urlparse(current_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    host_prefix = app.config["host_prefix"]
    return f"{base_url}{host_prefix}/annotate"


@app.template_filter("prettify_json")
def prettify_json(value):
    return json.dumps(value, sort_keys=True, indent=4, separators=(",", ": "))


# -----------------
# Decorators
# -----------------
# Very simple decorator to protect routes
def login_required(f):
    def wrapper(*args, **kwargs):
        if app.config["login"]["active"]:
            auth = request.cookies.get("auth")
            if not auth:
                return redirect(app.config["host_prefix"] + "/login")
            username, password = auth.split(":")
            if not workflows.check_login(app, username, password):
                return redirect(app.config["host_prefix"] + "/login")

        return f(*args, **kwargs)

    wrapper.__name__ = f.__name__
    return wrapper


# -----------------
# Flask endpoints
# -----------------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    logger.info(f"Main page loaded")

    return render_template(
        "pages/index.html",
        host_prefix=app.config["host_prefix"],
    )


@app.route("/analyze", methods=["GET", "POST"])
@login_required
def analyze():
    campaigns = workflows.get_sorted_campaign_list(app, modes=[CampaignMode.CROWDSOURCING, CampaignMode.LLM_EVAL])

    return render_template(
        "pages/analyze.html",
        campaigns=campaigns,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/analyze/detail/<campaign_id>", methods=["GET", "POST"])
@login_required
def analyze_detail(campaign_id):
    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    datasets = workflows.get_local_dataset_overview(app)

    statistics = analysis.compute_statistics(app, campaign, datasets)

    return render_template(
        "pages/analyze_detail.html",
        statistics=statistics,
        campaign=campaign,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/annotate/<campaign_id>", methods=["GET", "POST"])
def annotate(campaign_id):
    campaign = workflows.load_campaign(app, campaign_id=campaign_id)

    service = campaign.metadata["config"]["service"]
    service_ids = workflows.get_service_ids(service, request.args)

    metadata = campaign.metadata
    annotation_set = workflows.get_annotator_batch(app, campaign, service_ids)

    if not annotation_set:
        # no more available examples
        return render_template(
            "campaigns/closed.html",
            host_prefix=app.config["host_prefix"],
        )

    return render_template(
        f"campaigns/{campaign.campaign_id}/annotate.html",
        host_prefix=app.config["host_prefix"],
        annotation_set=annotation_set,
        annotator_id=service_ids["annotator_id"],
        metadata=metadata,
    )


@app.route("/browse", methods=["GET", "POST"])
@login_required
def browse():
    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    example_idx = request.args.get("example_idx")

    if dataset_id and split and example_idx:
        display_example = {"dataset": dataset_id, "split": split, "example_idx": int(example_idx)}
        logger.info(f"Browsing {display_example}")
    else:
        display_example = None

    datasets = workflows.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    if not datasets:
        return render_template(
            "pages/no_datasets.html",
            host_prefix=app.config["host_prefix"],
        )

    workflows.generate_annotation_index(app)

    return render_template(
        "pages/browse.html",
        display_example=display_example,
        datasets=datasets,
        host_prefix=app.config["host_prefix"],
        annotations=app.db["annotation_index"],
    )


@app.route("/clear_campaign", methods=["POST"])
@login_required
def clear_campaign():
    data = request.get_json()
    campaign_id = data.get("campaignId")

    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    campaign.clear_all_outputs()

    return utils.success()


@app.route("/clear_output", methods=["GET", "POST"])
@login_required
def clear_output():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    idx = int(data.get("idx"))

    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    campaign.clear_output(idx)

    return utils.success()


@app.route("/crowdsourcing", methods=["GET", "POST"])
@login_required
def crowdsourcing():
    llm_configs = workflows.load_configs(mode=CampaignMode.LLM_EVAL)
    crowdsourcing_configs = workflows.load_configs(mode=CampaignMode.CROWDSOURCING)
    campaigns = workflows.get_sorted_campaign_list(app, modes=[CampaignMode.CROWDSOURSING])

    return render_template(
        "pages/crowdsourcing.html",
        campaigns=campaigns,
        llm_configs=llm_configs,
        crowdsourcing_configs=crowdsourcing_configs,
        is_password_protected=app.config["login"]["active"],
        host_prefix=app.config["host_prefix"],
    )


@app.route("/crowdsourcing/detail/<campaign_id>", methods=["GET", "POST"])
@login_required
def crowdsourcing_detail(campaign_id):
    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    overview = campaign.get_overview()
    stats = campaign.get_stats()

    return render_template(
        "pages/crowdsourcing_detail.html",
        mode=CampaignMode.CROWDSOURCING,
        campaign_id=campaign_id,
        overview=overview,
        stats=stats,
        metadata=campaign.metadata,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/crowdsourcing/create", methods=["POST"])
@login_required
def crowdsourcing_create():
    data = request.get_json()

    campaign_id = slugify(data.get("campaignId"))
    campaign_data = data.get("campaignData")
    config = data.get("config")

    config = workflows.parse_crowdsourcing_config(config)
    workflows.crowdsourcing_campaign_new(app, campaign_id, config, campaign_data)

    return utils.success()


@app.route("/crowdsourcing/new", methods=["GET", "POST"])
@login_required
def crowdsourcing_new():
    datasets = workflows.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    model_outs = workflows.get_model_outputs_overview(app, datasets, non_empty=True)
    configs = workflows.load_configs(mode=CampaignMode.CROWDSOURCING)

    default_campaign_id = workflows.generate_default_id(app=app, mode=CampaignMode.CROWDSOURCING, prefix="campaign")

    return render_template(
        "pages/crowdsourcing_new.html",
        default_campaign_id=default_campaign_id,
        datasets=datasets,
        model_outs=model_outs,
        configs=configs,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/compute_agreement", methods=["POST"])
@login_required
def compute_agreement():
    data = request.get_json()
    combinations = data.get("combinations")
    selected_campaigns = data.get("selectedCampaigns")

    campaign_index = workflows.generate_campaign_index(app, force_reload=True)
    datasets = workflows.get_local_dataset_overview(app)

    try:
        results = analysis.compute_inter_annotator_agreement(
            app,
            selected_campaigns=selected_campaigns,
            combinations=combinations,
            campaigns=campaign_index,
            datasets=datasets,
        )
        return jsonify(results)
    except Exception as e:
        traceback.print_exc()
        return utils.error(f"Error while computing agreement: {e}")


@app.route("/delete_campaign", methods=["POST"])
@login_required
def delete_campaign():
    data = request.get_json()
    campaign_id = data.get("campaignId")

    shutil.rmtree(os.path.join(CAMPAIGN_DIR, campaign_id))
    symlink_path = os.path.join(TEMPLATES_DIR, "campaigns", campaign_id, "annotate.html")

    if os.path.exists(symlink_path):
        os.remove(symlink_path)

    return utils.success()


@app.route("/delete_dataset", methods=["POST"])
@login_required
def delete_dataset():
    data = request.get_json()
    dataset_id = data.get("datasetId")
    workflows.delete_dataset(app, dataset_id)

    return utils.success()


@app.route("/delete_model_outputs", methods=["POST"])
@login_required
def delete_model_outputs():
    data = request.get_json()

    # get dataset, split, setup
    dataset_id = data.get("dataset")
    split = data.get("split")
    setup_id = data.get("setup_id")

    dataset = app.db["datasets_obj"][dataset_id]
    workflows.delete_model_outputs(dataset, split, setup_id)

    return utils.success()


@app.route("/download_dataset", methods=["POST"])
@login_required
def download_dataset():
    data = request.get_json()
    dataset_id = data.get("datasetId")

    try:
        workflows.download_dataset(app, dataset_id)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error while downloading dataset: {e}"})

    return utils.success()


@app.route("/duplicate_config", methods=["POST"])
def duplicate_config():
    data = request.get_json()
    filename = data.get("filename")
    mode_from = data.get("modeFrom")
    mode_to = data.get("modeTo")
    campaign_id = data.get("campaignId")

    campaign_index = workflows.generate_campaign_index(app, force_reload=False)

    if mode_from == mode_to:
        campaign = campaign_index[campaign_id]
        config = campaign.metadata["config"]
    else:
        # currently we only support copying the annotation_span_categories between modes
        campaign = campaign_index[campaign_id]
        llm_config = campaign.metadata["config"]
        config = {"annotation_span_categories": llm_config["annotation_span_categories"]}

    workflows.save_config(filename, config, mode=mode_to)

    return utils.success()


@app.route("/duplicate_eval", methods=["POST"])
def duplicate_eval():
    data = request.get_json()
    mode = data.get("mode")
    campaign_id = data.get("campaignId")
    new_campaign_id = slugify(data.get("newCampaignId"))

    ret = workflows.duplicate_eval(app, mode, campaign_id, new_campaign_id)

    return ret


@app.route("/example", methods=["GET", "POST"])
def render_example():
    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    example_idx = max(int(request.args.get("example_idx")), 0)

    try:
        example_data = workflows.get_example_data(app, dataset_id, split, example_idx)
        return jsonify(example_data)
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Error while getting example data: {e}")
        logger.error(f"{dataset_id=}, {split=}, {example_idx=}")
        return utils.error(f"Error\n\t{e}\nwhile getting example data: {dataset_id=}, {split=}, {example_idx=}")


@app.route("/export_campaign_outputs", methods=["POST"])
@login_required
def export_campaign_outputs():
    data = request.get_json()
    campaign_id = data.get("campaign_id")

    return workflows.export_campaign_outputs(campaign_id)


@app.route("/export_dataset", methods=["POST"])
@login_required
def export_dataset():
    dataset_id = request.args.get("dataset_id")

    return workflows.export_dataset(app, dataset_id)


@app.route("/export_outputs", methods=["POST"])
@login_required
def export_outputs():
    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    setup_id = request.args.get("setup_id")

    return workflows.export_outputs(app, dataset_id, split, setup_id)


@app.route("/files/<path:filename>", methods=["GET", "POST"])
def download_file(filename):
    # serving external files for datasets
    return send_from_directory("data", filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if workflows.check_login(app, username, password):
            # redirect to the home page ("/")
            resp = make_response(redirect(app.config["host_prefix"] + "/"))
            resp.set_cookie("auth", f"{username}:{password}")
            return resp
        else:
            return "Login failed", 401
    return render_template("pages/login.html", host_prefix=app.config["host_prefix"])


@app.route("/llm_eval", methods=["GET", "POST"])
@app.route("/llm_gen", methods=["GET", "POST"])
@login_required
def llm_campaign():
    logger.info(f"LLM campaign page loaded")

    mode = utils.get_mode_from_path(request.path)

    campaigns = workflows.get_sorted_campaign_list(app, modes=[CampaignMode.LLM_EVAL, CampaignMode.LLM_GEN])
    llm_configs = workflows.load_configs(mode=mode)
    crowdsourcing_configs = workflows.load_configs(mode=CampaignMode.CROWDSOURCING)

    return render_template(
        f"pages/llm_campaign.html",
        mode=mode,
        llm_configs=llm_configs,
        crowdsourcing_configs=crowdsourcing_configs,
        campaigns=campaigns,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_eval/create", methods=["GET", "POST"])
@app.route("/llm_gen/create", methods=["GET", "POST"])
@login_required
def llm_campaign_create():
    mode = utils.get_mode_from_path(request.path)
    data = request.get_json()

    campaign_id = slugify(data.get("campaignId"))
    campaign_data = data.get("campaignData")
    config = data.get("config")

    if mode == CampaignMode.LLM_EVAL:
        config = workflows.parse_llm_eval_config(config)
    elif mode == CampaignMode.LLM_GEN:
        config = workflows.parse_llm_gen_config(config)

    datasets = app.db["datasets_obj"]

    try:
        workflows.llm_campaign_new(mode, campaign_id, config, campaign_data, datasets)
        workflows.load_campaign(app, campaign_id=campaign_id, mode=mode)
    except Exception as e:
        traceback.print_exc()
        return workflows.error(f"Error while creating campaign: {e}")

    return utils.success()


@app.route("/llm_eval/detail/<campaign_id>", methods=["GET", "POST"])
@app.route("/llm_gen/detail/<campaign_id>", methods=["GET", "POST"])
@login_required
def llm_campaign_detail(campaign_id):
    mode = utils.get_mode_from_path(request.path)
    campaign = workflows.load_campaign(app, campaign_id=campaign_id)

    if campaign.metadata["status"] == CampaignStatus.RUNNING and not app.db["announcers"].get(campaign_id):
        campaign.metadata["status"] = CampaignStatus.IDLE
        campaign.update_metadata()

    overview = campaign.get_overview()
    finished_examples = [x for x in overview if x["status"] == ExampleStatus.FINISHED]

    return render_template(
        f"pages/llm_campaign_detail.html",
        mode=mode,
        campaign_id=campaign_id,
        overview=overview,
        finished_examples=finished_examples,
        metadata=campaign.metadata,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_eval/new", methods=["GET", "POST"])
@app.route("/llm_gen/new", methods=["GET", "POST"])
@login_required
def llm_campaign_new():
    mode = utils.get_mode_from_path(request.path)

    datasets = workflows.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    non_empty = True if mode == "llm_eval" else False
    model_outs = workflows.get_model_outputs_overview(app, datasets, non_empty=non_empty)

    # get a list of available metrics
    llm_configs = workflows.load_configs(mode=mode)
    metric_types = list(ModelFactory.model_classes()[mode].keys())

    default_campaign_id = workflows.generate_default_id(app, mode=mode, prefix=mode.replace("_", "-"))

    return render_template(
        f"pages/llm_campaign_new.html",
        mode=mode,
        datasets=datasets,
        default_campaign_id=default_campaign_id,
        model_outs=model_outs,
        configs=llm_configs,
        metric_types=metric_types,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_eval/run", methods=["POST"])
@app.route("/llm_gen/run", methods=["POST"])
@login_required
def llm_campaign_run():
    mode = utils.get_mode_from_path(request.path)
    data = request.get_json()
    campaign_id = data.get("campaignId")

    app.db["announcers"][campaign_id] = announcer = workflows.MessageAnnouncer()
    app.db["threads"][campaign_id] = {
        "running": True,
    }

    try:
        campaign = workflows.load_campaign(app, campaign_id=campaign_id)
        threads = app.db["threads"]
        datasets = app.db["datasets_obj"]

        config = campaign.metadata["config"]
        model = ModelFactory.from_config(config, mode=mode)

        return workflows.run_llm_campaign(mode, campaign_id, announcer, campaign, datasets, model, threads)
    except Exception as e:
        traceback.print_exc()
        return workflows.error(f"Error while running campaign: {e}")


@app.route("/llm_campaign/update_metadata", methods=["POST"])
@login_required
def llm_campaign_update_config():
    data = request.get_json()

    campaign_id = data.get("campaignId")
    config = data.get("config")

    config = workflows.parse_campaign_config(config)
    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    campaign.metadata["config"] = config
    campaign.update_metadata()

    return utils.success()


@app.route("/llm_campaign/progress/<campaign_id>", methods=["GET", "POST"])
@login_required
def listen(campaign_id):
    if not app.db["announcers"].get(campaign_id):
        return Response(status=404)

    def stream():
        messages = app.db["announcers"][campaign_id].listen()
        while True:
            msg = messages.get()
            yield msg

    return Response(stream(), mimetype="text/event-stream")


@app.route("/llm_campaign/pause", methods=["POST"])
@login_required
def llm_campaign_pause():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    app.db["threads"][campaign_id]["running"] = False

    campaign = workflows.load_campaign(app, campaign_id=campaign_id)
    campaign.metadata["status"] = CampaignStatus.IDLE
    campaign.update_metadata()

    resp = jsonify(success=True, status=campaign.metadata["status"])
    return resp


@app.route("/manage", methods=["GET", "POST"])
@login_required
def manage():
    datasets = workflows.get_local_dataset_overview(app)
    dataset_classes = list(get_dataset_classes().keys())

    datasets_enabled = {k: v for k, v in datasets.items() if v["enabled"]}
    model_outputs = workflows.get_model_outputs_overview(app, datasets_enabled)

    datasets_for_download = workflows.get_datasets_for_download(app)

    # set as `downloaded` the datasets that are already downloaded
    for dataset_id in datasets_for_download.keys():
        datasets_for_download[dataset_id]["downloaded"] = dataset_id in datasets

    campaigns = workflows.get_sorted_campaign_list(
        app, modes=[CampaignMode.CROWDSOURCING, CampaignMode.LLM_EVAL, CampaignMode.LLM_GEN, CampaignMode.EXTERNAL]
    )

    return render_template(
        "pages/manage.html",
        datasets=datasets,
        dataset_classes=dataset_classes,
        datasets_for_download=datasets_for_download,
        host_prefix=app.config["host_prefix"],
        model_outputs=model_outputs,
        campaigns=campaigns,
    )


@app.route("/save_config", methods=["POST"])
def save_config():
    data = request.get_json()
    filename = data.get("filename")
    config = data.get("config")
    mode = data.get("mode")

    if mode == CampaignMode.LLM_EVAL:
        config = workflows.parse_llm_eval_config(config)
    elif mode == CampaignMode.LLM_GEN:
        config = workflows.parse_llm_gen_config(config)
    elif mode == CampaignMode.CROWDSOURCING:
        config = workflows.parse_crowdsourcing_config(config)
    else:
        return utils.error(f"Invalid mode: {mode}")

    workflows.save_config(filename, config, mode=mode)

    return utils.success()


@app.route("/save_generation_outputs", methods=["GET", "POST"])
@login_required
def save_generation_outputs():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    model_name = slugify(data.get("modelName"))

    workflows.save_generation_outputs(app, campaign_id, model_name)

    return utils.success()


@app.route("/submit_annotations", methods=["POST"])
def submit_annotations():
    logger.info(f"Received annotations")
    data = request.get_json()
    campaign_id = data["campaign_id"]
    annotation_set = data["annotation_set"]
    annotator_id = data["annotator_id"]

    return workflows.save_annotations(app, campaign_id, annotation_set, annotator_id)


@app.route("/set_dataset_enabled", methods=["POST"])
@login_required
def set_dataset_enabled():
    data = request.get_json()
    dataset_id = data.get("datasetId")
    enabled = data.get("enabled")

    workflows.set_dataset_enabled(app, dataset_id, enabled)

    return utils.success()


@app.route("/upload_dataset", methods=["POST"])
@login_required
def upload_dataset():
    data = request.get_json()
    dataset_id = data.get("id")
    dataset_description = data.get("description")
    dataset_format = data.get("format")
    dataset_data = data.get("dataset")

    try:
        workflows.upload_dataset(app, dataset_id, dataset_description, dataset_format, dataset_data)
    except Exception as e:
        traceback.print_exc()
        return utils.error(f"Error while uploading dataset: {e}")

    return utils.success()


@app.route("/upload_model_outputs", methods=["POST"])
@login_required
def upload_model_outputs():
    logger.info(f"Received model outputs")
    data = request.get_json()
    dataset_id = data["dataset"]
    split = data["split"]
    setup_id = data["setup_id"]
    model_outputs = data["outputs"]

    dataset = app.db["datasets_obj"][dataset_id]

    try:
        workflows.upload_model_outputs(dataset, split, setup_id, model_outputs)
    except Exception as e:
        traceback.print_exc()
        return utils.error(f"Error while adding model outputs: {e}")

    return utils.success()
