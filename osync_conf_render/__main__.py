#!/usr/bin/env python3
import argparse
import functools
import itertools
import logging
import os
import shutil
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any

import jinja2
from deepmerge import Merger
from ruamel.yaml import YAML

yaml = YAML(typ="safe")
merger = Merger(
    # pass in a list of tuple, with the
    # strategies you are looking to apply
    # to each type.
    [(list, ["append"]), (dict, ["merge"]), (set, ["union"])],
    # next, choose the fallback strategies,
    # applied to all other types:
    ["override"],
    # finally, choose the strategies in
    # the case where the types conflict:
    ["override"],
)

logger = logging.getLogger("osync_conf_render")

path_root = Path(__file__).parent.parent.resolve()
path_templates = path_root / "templates"
path_config_template = path_templates / "osync.conf.j2"

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--local_host", type=str, required=False)

assert path_config_template.is_file()


def render(config: Path, local_host: str | None = None):
    logger.info(f"Reading config from path: {config}")
    if local_host is not None:
        logger.info(f"Creating configs for local_host (as specified): {local_host}")
    else:
        local_host = local_host or socket.gethostname().split('.')[0]
        logger.info(f"Creating configs for local_host (from hostname): {local_host}")

    # read template
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=str(path_templates)),
    )
    env.globals["str"] = str
    config_template = env.get_template(path_config_template.name)

    # delete old configs
    configs_path = (config.parent / "configs").resolve()
    if configs_path.is_dir():
        logger.warning("Deleting old configs")
        shutil.rmtree(configs_path)

    configs_path.mkdir()

    # read config
    data = yaml.load(config)
    if data is None:
        logger.error("Couldn't read config!")
        return

    # read default args used as base for merging
    main_args: dict[str, Any] = data.get("args", None)
    assert main_args is not None, f".args missing"
    assert type(main_args) is dict, f".args is not dict"

    # read jobs
    jobs: list[dict[str, Any]] = data.get("jobs", None)
    assert jobs is not None, f".jobs missing"
    assert type(jobs) is list, f".jobs is not list"
    assert len(jobs) > 0, f".jobs empty"

    for job_i, job in enumerate(jobs):
        # read id of this sync job
        job_id: str = job.get("id", None)
        assert (
            job_id is not None and len(job_id.strip()) > 0
        ), f"jobs[{job_i}].id missing"
        job_id = job_id.strip()

        # read job args
        job_args: dict[str, Any] = job.get("args", None)
        assert job_args is not None, f".jobs[{job_i}]({job_id}).args missing"
        assert type(job_args) is dict, f".jobs[{job_i}]({job_id}).args is not a dict"

        # read hosts
        hosts: list[dict[str, Any]] = job.get("hosts", None)
        assert hosts is not None, f".jobs[{job_i}]({job_id}).hosts missing"
        assert type(hosts) is list, f".jobs[{job_i}]({job_id}).hosts is not a list"
        assert len(hosts) > 0, f".jobs[{job_i}]({job_id}).hosts empty"

        for host_i, host in enumerate(hosts):
            assert (
                type(host) is dict
            ), f".jobs[{job_i}]({job_id}).hosts[{host_i}] is not a dict"

            host_id: str = host.get("id", None)
            assert (
                host_id is not None and len(host_id.strip()) > 0
            ), f".jobs[{job_i}]({job_id}).hosts[{host_i}].id missing"
            host_id = host_id.strip()

            # filter hosts to only consider the current running machine for variant generation
            # as else absolute paths might be misleading
            if local_host != host_id:
                logger.info(f"Skipping config variants for host {host_id}")
                continue
            else:
                logger.info(f"Creating config variants for host {host_id}")

            # read host args
            host_args: dict[str, Any] = host.get("args", None)
            assert (
                host_args is not None
            ), f".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).args missing"
            assert (
                type(host_args) is dict
            ), f".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).args is not a dict"

            # read variants of this host
            host_variants: list[dict[str, Any]] = host.get("variants", None)
            assert (
                host_variants is not None
            ), ".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).variants is missing"
            assert (
                type(host_variants) is list
            ), ".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).variants is not a list"
            assert (
                len(host_variants) > 0
            ), ".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).variants is empty"

            for variant_i, variant in enumerate(host_variants):
                variant_id: str = variant.get("id", None)
                assert (
                    variant_id is not None and len(variant_id.strip()) > 0
                ), f".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id}).variants[{variant_i}].id missing"
                variant_id = variant_id.strip()

                variant_args: dict[str, Any] = variant.get("args", None)
                assert variant_args is not None, (
                    f".jobs[{job_i}]({job_id}).hosts[{host_i}]({host_id})"
                    f".variants[{variant_id}]({variant_id}).args are missing"
                )

                variant_merged_args = functools.reduce(
                    lambda base, nxt: merger.merge(deepcopy(base), nxt),
                    [main_args, job_args, host_args, variant_args],
                )

                variant_config_path = (
                    configs_path / f"{job_id}/{host_id}/{variant_id}/osync.conf"
                )
                variant_config_path.parent.mkdir(parents=True, exist_ok=True)

                variant_context = dict(
                    args=variant_merged_args,
                    id=f"{job_id}_{host_id}_{variant_id}",
                    variant_config_path=str(variant_config_path),
                    main_id=job_id,
                    host_id=host_id,
                    variant_id=variant_id,
                )

                # consolidate multiple include / exclude from files into one file
                def agg_filters_from(
                    path_str_list: list[str], agg_path: Path
                ) -> Path | None:
                    logger.debug(f"Aggregating from {path_str_list} to {agg_path}...")
                    if len(path_str_list) > 0:
                        agg_text = ""

                        for from_path_str in path_str_list:
                            logger.debug(f"Reading from {from_path_str}...")
                            from_path = Path(from_path_str)
                            if not from_path.is_absolute():
                                from_path = config.parent / from_path
                            from_path = from_path.resolve()
                            agg_text += from_path.read_text()
                            agg_text += os.linesep

                        agg_path.write_text(agg_text)
                        return agg_path
                    return None

                include_from_path_list: list[str] = (
                    variant_context.get("args", {})
                    .get("filters", {})
                    .get("include", {})
                    .get("from", [])
                )
                exclude_from_path_list: list[str] = (
                    variant_context.get("args", {})
                    .get("filters", {})
                    .get("exclude", {})
                    .get("from", [])
                )

                include_from_path = agg_filters_from(
                    include_from_path_list,
                    variant_config_path.parent / "include_from_agg.txt",
                )
                exclude_from_path = agg_filters_from(
                    exclude_from_path_list,
                    variant_config_path.parent / "exclude_from_agg.txt",
                )

                if include_from_path is not None:
                    variant_context.get("args", {}).get("filters", {}).get(
                        "include", {}
                    )["from_agg"] = str(include_from_path)

                if exclude_from_path is not None:
                    variant_context.get("args", {}).get("filters", {}).get(
                        "exclude", {}
                    )["from_agg"] = str(exclude_from_path)

                logger.info(f"Creating osync config {variant_config_path}")
                variant_config_path.write_text(
                    config_template.render(**variant_context)
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    render(**vars(parser.parse_args()))
