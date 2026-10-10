"""Kubernetes client setup shared by Kira's in-process tools."""

import functools


@functools.cache
def load_config():
    """Use the pod's service account in the cluster, or ~/.kube/config locally."""
    from kubernetes import config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def apps_api():
    from kubernetes import client

    load_config()
    return client.AppsV1Api()


def core_api():
    from kubernetes import client

    load_config()
    return client.CoreV1Api()


def custom_api():
    from kubernetes import client

    load_config()
    return client.CustomObjectsApi()
