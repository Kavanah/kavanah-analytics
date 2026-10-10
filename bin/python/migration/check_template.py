#!/usr/bin/env python3
"""Quick script to check the index template pattern"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client
import json

es = get_elasticsearch_client()
if es:
    template = es.indices.get_index_template(name="default_replicas_0_for_single_node")
    print(json.dumps(template, indent=2))
