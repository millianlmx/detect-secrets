import argparse
import inspect
import os
from importlib import import_module
from urllib.parse import urlparse

from ... import filters
from ...constants import VerifiedResult
from ...core.log import log
from ...exceptions import InvalidFile
from ...settings import get_settings
from ...util.importlib import import_file_as_module
from .common import valid_path


def add_filter_options(parent: argparse.ArgumentParser) -> None:
    parser = parent.add_argument_group(
        title='filter options',
        description=(
            'Configure settings for filtering out secrets after they are flagged '
            'by the engine.'
        ),
    )

    verify_group = parser.add_mutually_exclusive_group()
    verify_group.add_argument(
        '-n',
        '--no-verify',
        action='store_true',
        help='Disables additional verification of secrets via network call.',
    )
    verify_group.add_argument(
        '--only-verified',
        action='store_true',
        help='Only flags secrets that can be verified.',
    )

    parser.add_argument(
        '--exclude-lines',
        type=str,
        action='append',
        help='If lines match this regex, it will be ignored.',
    )
    parser.add_argument(
        '--exclude-files',
        type=str,
        action='append',
        help='If filenames match this regex, it will be ignored.',
    )
    parser.add_argument(
        '--exclude-secrets',
        type=str,
        action='append',
        help='If secrets match this regex, it will be ignored.',
    )

    if filters.wordlist.is_feature_enabled():
        parser.add_argument(
            '--word-list',
            type=valid_path,
            help=(
                'Text file with a list of words, '
                'if a secret contains a word in the list we ignore it.'
            ),
            dest='word_list_file',
        )

    if filters.gibberish.is_feature_enabled():
        parser.add_argument(
            '--gibberish-model',
            type=valid_path,
            help='Path to model trained with gibberish-detector.',
            dest='gibberish_model_file',
        )
        parser.add_argument(
            '--gibberish-limit',
            type=float,
            help='Threshold to determine whether a string is gibberish.',
        )

    if filters.classifier.is_feature_enabled():
        parser.add_argument(
            '--huggingface-model',
            type=str,
            help='HuggingFace model path for classifying secrets.',
        )
        parser.add_argument(
            '--threshold',
            type=float,
            help='Threshold to determine whether a string is a secret.',
        )
        parser.add_argument(
            '--huggingface-token',
            type=str,
            help='Huggingface API token for downloading models.',
        )

    _add_custom_filters(parser)
    _add_disable_flag(parser)


def _add_custom_filters(parser: argparse._ArgumentGroup) -> None:
    def valid_looking_paths(path: str) -> str:
        # Expected path format:
        #   - detect_secrets.filters.common.is_invalid_file (python import path)
        #   - testing/custom_filters.py::is_invalid_secret (local file)
        #   - file://testing/custom_filters.py::is_invalid_secret (local file)
        parts = urlparse(path)
        if not parts.scheme and '::' in path:
            # This could be a local file, without the file schema.
            path = 'file://' + path
            parts = urlparse(path)

        if parts.scheme == 'file':
            # May be local file.
            # We do some initial pre-processing, but perform the file validation during the
            # post-processing step.
            components = parts.path.split('::')
            if len(components) != 2:
                raise argparse.ArgumentTypeError(
                    'Did not specify function name for imported file.',
                )

            file_path = path[len('file://'):].split('::')[0]
            if not os.path.isfile(file_path):
                raise argparse.ArgumentTypeError(f'{file_path} is not a valid file.')
        elif parts.scheme:
            raise argparse.ArgumentTypeError(f'{path} is not a valid filter path.')

        return path

    parser.add_argument(
        '-f',
        '--filter',
        type=valid_looking_paths,
        nargs=1,
        action='append',        # so we can support multiple flags with same value
        help=(
            'Specify path to custom filter. '
            'May be a python module path (e.g. detect_secrets.filters.common.is_invalid_file) or '
            'a local file path (e.g. file://path/to/file.py::function_name).'
        ),
    )


def _add_disable_flag(parser: argparse._ArgumentGroup) -> None:
    parser.add_argument(
        '--disable-filter',
        type=str,
        nargs=1,
        action='append',        # so we can support multiple flags with same value
        help='Specify filter to disable. e.g. detect_secrets.filters.common.is_invalid_file',
    )


def parse_args(args: argparse.Namespace) -> None:
    if args.exclude_lines:
        get_settings().filters['detect_secrets.filters.regex.should_exclude_line'] = {
            'pattern': args.exclude_lines,
        }

    if args.exclude_files:
        get_settings().filters['detect_secrets.filters.regex.should_exclude_file'] = {
            'pattern': args.exclude_files,
        }

    if args.exclude_secrets:
        get_settings().filters['detect_secrets.filters.regex.should_exclude_secret'] = {
            'pattern': args.exclude_secrets,
        }

    if (
        filters.wordlist.is_feature_enabled()
        and args.word_list_file
    ):
        filters.wordlist.initialize(args.word_list_file)

    if filters.gibberish.is_feature_enabled():
        kwargs = {}
        if args.gibberish_model_file:
            kwargs['model_path'] = args.gibberish_model_file

        if args.gibberish_limit:
            kwargs['limit'] = args.gibberish_limit

        filters.gibberish.initialize(**kwargs)

    if filters.classifier.is_feature_ready(args):
        kwargs = {}
        if args.huggingface_model:
            kwargs['huggingface_model'] = args.huggingface_model

        if args.threshold:
            kwargs['threshold'] = args.threshold

        if args.huggingface_token:
            kwargs['huggingface_token'] = args.huggingface_token

        import torch

        if torch.cuda.is_available():
            args.num_cores = [3]
        else:
            args.num_cores = [1]

        import torch.multiprocessing as mp
        mp.set_start_method('spawn', force=True)

        filters.classifier.initialize(**kwargs)

    if not args.no_verify:
        get_settings().filters[
            'detect_secrets.filters.common.is_ignored_due_to_verification_policies'
        ] = {
            'min_level': (
                VerifiedResult.VERIFIED_TRUE
                if args.only_verified
                else VerifiedResult.UNVERIFIED
            ).value,
        }
    else:
        get_settings().disable_filters(
            'detect_secrets.filters.common.is_ignored_due_to_verification_policies',
        )

    if args.disable_filter:
        # Flatten entry for easier parsing.
        args.disable_filter = [entry for item in args.disable_filter for entry in item]

        redundant_disabled_filters = set(args.disable_filter) - set(get_settings().filters)
        for name in redundant_disabled_filters:
            log.warning(f'Redundant --disable-filter "{name}"')

        get_settings().disable_filters(*args.disable_filter)

    if args.filter:
        # Flatten entry for easier parsing.
        args.filter = [entry for item in args.filter for entry in item]

        # Post-processing validation
        for item in args.filter:
            _raise_if_custom_filter_path_is_invalid(item)
            get_settings().filters[item] = {}


def _raise_if_custom_filter_path_is_invalid(path: str) -> None:
    """Performs post-validation for custom filters."""
    parts = urlparse(path)
    if not parts.scheme:
        try:
            module_path, function_name = path.rsplit('.', 1)
        except ValueError:
            raise argparse.ArgumentTypeError(
                'Invalid Python module path for custom filter.',
            )

        try:
            module = import_module(module_path)
        except ModuleNotFoundError:
            raise argparse.ArgumentTypeError(f'Cannot import "{path}" as custom filter.')

        try:
            function = getattr(module, function_name)
        except AttributeError:
            raise argparse.ArgumentTypeError(
                f'No filter function named `{function_name}` found in "{module_path}".',
            )

        if not inspect.isfunction(function):
            raise argparse.ArgumentTypeError(f'{path} is not a filter function.')

    elif parts.scheme == 'file':
        file_path, function_name = path[len('file://'):].split('::')

        try:
            module = import_file_as_module(file_path)
        except (FileNotFoundError, InvalidFile):
            raise argparse.ArgumentTypeError(
                f'Cannot import {file_path} as custom filter.',
            )

        try:
            getattr(module, function_name)
        except AttributeError:
            raise argparse.ArgumentTypeError(
                f'No filter function named `{function_name}` found in "{file_path}".',
            )
