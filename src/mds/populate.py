import asyncio
from argparse import Namespace
from typing import Any, Dict, List
from mds.agg_mds import datastore, adapters
from mds.agg_mds.mds import pull_mds
from mds.agg_mds.commons import MDSInstance, ColumnsToFields, Commons, parse_config
from mds import config, logger
from pathlib import Path
from urllib.parse import urlparse
import argparse
import sys
import json


def parse_args(argv: List[str]) -> Namespace:
    """
    Parse argument from command line in this case the input file to read from
    the output file to write to (if needed) and the command
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="config file to use", type=str, required=True)
    parser.add_argument(
        "--hostname", help="hostname of server", type=str, default="localhost"
    )
    parser.add_argument("--port", help="port of server", type=int, default=6379)
    known_args, unknown_args = parser.parse_known_args(argv)
    return known_args


async def populate_metadata(name: str, common, results):
    mds_arr = [{k: v} for k, v in results.items()]

    total_items = len(mds_arr)

    if total_items == 0:
        logger.warning(f"populating {name} aborted as there are no items to add")
        return

    # prefilter to remove entries not matching a certain field.
    if hasattr(common, "select_field") and common.select_field is not None:
        mds_arr = await filter_entries(common, mds_arr)

    tags = {}

    # inject common_name field into each entry
    for x in mds_arr:
        key = next(iter(x.keys()))
        entry = next(iter(x.values()))

        def normalize(entry: dict) -> Any:
            if (
                not hasattr(common, "columns_to_fields")
                or common.columns_to_fields is None
            ):
                return entry

            for column, field in common.columns_to_fields.items():
                if field == column:
                    continue
                if isinstance(field, ColumnsToFields):
                    entry[common.study_data_field][column] = field.get_value(
                        entry[common.study_data_field]
                    )
                else:
                    if field in entry[common.study_data_field]:
                        entry[common.study_data_field][column] = entry[
                            common.study_data_field
                        ][field]
            return entry

        entry = normalize(entry)

        # add the common field and url to the entry
        entry[common.study_data_field]["commons_name"] = name

        # add to tags
        for t in entry[common.study_data_field]["tags"]:
            if t["category"] not in tags:
                tags[t["category"]] = set()
            tags[t["category"]].add(t["name"])

    # process tags set to list
    for k, v in tags.items():
        tags[k] = list(tags[k])

    keys = list(results.keys())
    info = {"commons_url": common.commons_url}

    # build ES normalization dictionary
    field_typing = {
        field: "object" if info.type in ["nested", "array"] else info.type
        for field, info in commons.fields.items()
    }

    await datastore.update_metadata(
        name, mds_arr, keys, tags, info, field_typing, common.study_data_field
    )


async def populate_info(commons_config: Commons) -> None:
    agg_info = {
        key: value.to_dict() for key, value in commons_config.aggregations.items()
    }
    await datastore.update_global_info("aggregations", agg_info)


async def populate_config(commons_config: Commons) -> None:
    array_definition = {
        "array": [
            field for field, value in commons.fields.items() if value.type == "array"
        ]
    }
    await datastore.update_config_info(array_definition)


async def main(commons_config: Commons) -> None:
    """
    Given a config structure, pull all metadata from each one in the config and cache into the following
    structure:
    {
       "commons_name" : {
            "metadata" : [ array of metadata entries ],
            "field_mapping" : { dictionary of field_name to column_name },
            "guids: [ array of guids, used to index into the metadata array ],
            "tags": { 'category' : [ values ] },
            "commons_url" : "url of commons portal"
        },
        "..." : {
        }
    """

    if not config.USE_AGG_MDS:
        logger.info("aggregate MDS disabled")
        exit(1)

    url_parts = urlparse(config.ES_ENDPOINT)

    await datastore.init(hostname=url_parts.hostname, port=url_parts.port)

    # build mapping table for commons index

    field_mapping = {
        "mappings": {
            "commons": {
                "properties": {
                    field: {"type": {"array": "nested"}.get(info.type, info.type)}
                    for field, info in commons.fields.items()
                    if info.type in ["array", "nested"]
                }
            }
        }
    }

    await datastore.drop_all(commons_mapping=field_mapping)

    for name, common in commons_config.gen3_commons.items():
        logger.info(f"Populating {name} using Gen3 MDS connector")
        results = pull_mds(common.mds_url, common.guid_type)
        logger.info(f"Received {len(results)} from {name}")
        if len(results) > 0:
            await populate_metadata(name, common, results)

    for name, common in commons_config.adapter_commons.items():
        logger.info(f"Populating {name} using adapter: {common.adapter}")
        results = adapters.get_metadata(
            common.adapter,
            common.mds_url,
            common.filters,
            common.config,
            common.field_mappings,
            common.per_item_values,
            common.keep_original_fields,
            common.global_field_filters,
        )
        logger.info(f"Received {len(results)} from {name}")
        if len(results) > 0:
            await populate_metadata(name, common, results)

    # populate global information index
    await populate_info(commons_config)
    # populate array index information to support guppy
    await populate_config(commons_config)
    res = await datastore.get_status()
    print(res)
    await datastore.close()


async def filter_entries(
    common: MDSInstance, mds_arr: List[Dict[Any, Any]]
) -> List[Dict[Any, Any]]:
    """
    Filter metadata based on the select filter object defined in
    the config file. An example:
        "select_field": {
            "field_name" : "commons" ,
            "field_value" : "Proteomic Data Commons"
        }
        where only the records with the commons field === "Proteomic Data Commons" are added.
        Note the function assumes the field exists in all of the entries in the mds_arr parameter
    """
    filtered = []
    for x in mds_arr:
        key = next(iter(x.keys()))
        entry = next(iter(x.values()))
        if common.study_data_field not in entry:
            logger.warning(
                f'"{common.study_data_field}" not found in response from {common.mds_url} for {common.commons_url}. Skipping.'
            )
            continue
        value = entry[common.study_data_field].get(
            common.select_field["field_name"], None
        )
        if value is None or common.select_field["field_value"] != value:
            continue
        filtered.append({key: entry})
    logger.info(
        f"Loaded {len(filtered)} entries from {common.mds_url} for {common.commons_url}."
    )
    return filtered


def parse_config_from_file(path: Path) -> Commons:
    with open(path, "rt") as infile:
        return parse_config(json.load(infile))


if __name__ == "__main__":
    """
    Runs a "populate" procedure. Assumes the datastore is ready.
    """
    args: Namespace = parse_args(sys.argv)
    commons = parse_config_from_file(Path(args.config))
    asyncio.run(main(commons_config=commons))
