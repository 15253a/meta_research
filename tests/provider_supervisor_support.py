"""Synthetic receipt facts for consumer tests, not process-termination evidence."""

import sys

from meta_research.provider_supervisor import supervisor_host_identity


def closed_tree_fixture():
    return {
        "schema_ref": "meta-research/provider-process-tree/v1",
        "state": "closed",
        "mechanism": "linux-subreaper-echild" if sys.platform == "linux" else "windows-job",
        **supervisor_host_identity(),
    }
