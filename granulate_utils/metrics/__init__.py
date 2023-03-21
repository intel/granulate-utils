#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#
# (C) Datadog, Inc. 2018-present. All rights reserved.
# Licensed under a 3-clause BSD style license (see LICENSE.bsd3).
#
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Tuple, Union
from urllib.parse import urljoin

import requests


@dataclass
class Sample:
    # The field names match the schema expected by the server one-to-one, so we can get a JSON-able
    # dict simply by accessing __dict__.
    labels: Dict[str, str]
    name: str  # metric name
    value: Union[int, float]


@dataclass
class MetricsSnapshot:
    timestamp: datetime
    samples: Tuple[Sample, ...]


def rest_request(url: str, **kwargs: Any) -> requests.Response:
    """
    Query the given URL and return the response
    """
    response = requests.get(url, params={k: v for k, v in kwargs.items() if v is not None}, timeout=3)
    response.raise_for_status()
    return response


def json_request(url: str, **kwargs) -> Any:
    """
    Query the given URL using HTTP GET and return the JSON response.
    :param kwargs: request parameters
    """
    return rest_request(url, **kwargs).json()


def rest_request_to_json(url: str, object_path: str, *args: Any, **kwargs: Any) -> Any:
    """
    Query url/object_path/args/... and return the JSON response
    """
    if object_path:
        url = join_url_dir(url, object_path)

    # Add args to the url
    if args:
        for directory in args:
            url = join_url_dir(url, directory)

    return json_request(url, **kwargs)


def join_url_dir(url: str, *args: Any) -> str:
    """
    Join a URL with multiple directories
    """
    for path in args:
        url = url.rstrip("/") + "/"
        url = urljoin(url, path.lstrip("/"))
    return url


def set_individual_metric(
    collected_metrics: Dict[str, Dict[str, Any]], name: str, value: Any, labels: Dict[str, str]
) -> None:
    """
    Add a metric to collected_metrics with labels in {name, value, labels} format.
    Metric is only added if the value is not None.
    """
    assert name not in collected_metrics, f"attempted to add metric {name!r} twice!"

    if value is not None:
        collected_metrics[name] = {
            "name": name,
            "value": value,
            "labels": labels,
        }


def set_metrics_from_json(
    collected_metrics: Dict[str, Dict[str, Any]],
    labels: Dict[str, str],
    metrics_json: Dict[Any, Any],
    metrics: Dict[str, str],
) -> None:
    """
    Extract metrics values from JSON response and add to collected_metrics in {name, value, labels} format.
    """
    if metrics_json is None:
        return

    for field_name, metric_name in metrics.items():
        metric_value = metrics_json.get(field_name)
        set_individual_metric(collected_metrics, metric_name, metric_value, labels)


def samples_from_json(
    labels: Dict[str, str], response_json: Dict[Any, Any], metrics: Dict[str, str]
) -> Iterable[Sample]:
    """
    Parse the JSON response and set the metrics
    """
    if response_json is None:
        return

    for field_name, metric_name in metrics.items():
        if (value := response_json.get(field_name)) is not None:
            yield Sample(labels, metric_name, value)
