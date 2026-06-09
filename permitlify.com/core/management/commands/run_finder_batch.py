import json
import logging
import os
import sys
import time
from datetime import datetime as _dt

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run a finder batch worker in a standalone process.'

    def add_arguments(self, parser):
        parser.add_argument('batch_id', type=int)
        parser.add_argument('--model', type=str, default=None)
        parser.add_argument('--max-tokens', type=int, default=None)
        parser.add_argument('--prompt-template', type=str, default=None)

    def handle(self, *args, **options):
        batch_id = options['batch_id']
        settings = {
            'model': options.get('model'),
            'max_tokens': options.get('max_tokens'),
            'prompt_template': options.get('prompt_template'),
        }

        from core.db import update_finder_batch
        update_finder_batch(batch_id, thread_name=f'pid:{os.getpid()}')

        from core.views import _finder_batch_worker
        _finder_batch_worker(batch_id, settings)
