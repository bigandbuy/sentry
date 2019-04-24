from __future__ import absolute_import

import logging
import jsonschema
import six
import sys
import time

from django.core.urlresolvers import reverse
from requests import Session
from requests.exceptions import RequestException
from six.moves.urllib.parse import urljoin

from sentry import options
from sentry.attachments import attachment_cache
from sentry.auth.system import get_system_token
from sentry.cache import default_cache
from sentry.coreapi import cache_key_for_event
from sentry.lang.native.cfi import reprocess_minidump_with_cfi
from sentry.lang.native.minidump import MINIDUMP_ATTACHMENT_TYPE
from sentry.lang.native.symbolizer import FATAL_ERRORS, USER_FIXABLE_ERRORS
from sentry.lang.native.utils import image_name
from sentry.lang.native.unreal import parse_portable_callstack
from sentry.models import EventError, Project
from sentry.reprocessing import report_processing_issue
from sentry.stacktraces import find_stacktraces_in_data
from sentry.tasks.store import RetrySymbolication
from sentry.utils import json, metrics
from sentry.utils.cache import memoize
from sentry.utils.in_app import is_known_third_party, is_optional_package
from sentry.utils.safe import get_path, setdefault_path


logger = logging.getLogger(__name__)


MAX_ATTEMPTS = 3
BACKOFF_FACTOR = 1.6
REQUEST_CACHE_TIMEOUT = 3600
SYMBOLICATOR_TIMEOUT = 5

BUILTIN_SOURCES = {
    'microsoft': {
        'type': 'http',
        'id': 'sentry:microsoft',
        'layout': {'type': 'symstore'},
        'filetypes': ['pdb', 'pe'],
        'url': 'https://msdl.microsoft.com/download/symbols/',
        'is_public': True,
    },
}

VALID_LAYOUTS = (
    'native',
    'symstore',
)

VALID_FILE_TYPES = (
    'pe',
    'pdb',
    'mach_debug',
    'mach_code',
    'elf_debug',
    'elf_code',
    'breakpad',
)

VALID_CASINGS = (
    'lowercase',
    'uppercase',
    'default'
)

LAYOUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'type': {
            'type': 'string',
            'enum': list(VALID_LAYOUTS),
        },
        'casing': {
            'type': 'string',
            'enum': list(VALID_CASINGS),
        },
    },
    'required': ['type'],
    'additionalProperties': False,
}

COMMON_SOURCE_PROPERTIES = {
    'id': {
        'type': 'string',
        'minLength': 1,
    },
    'layout': LAYOUT_SCHEMA,
    'filetypes': {
        'type': 'array',
        'items': {
            'type': 'string',
            'enum': list(VALID_FILE_TYPES),
        }
    },
}


S3_SOURCE_SCHEMA = {
    'type': 'object',
    'properties': dict(
        type={
            'type': 'string',
            'enum': ['s3'],
        },
        bucket={'type': 'string'},
        region={'type': 'string'},
        access_key={'type': 'string'},
        secret_key={'type': 'string'},
        prefix={'type': 'string'},
        **COMMON_SOURCE_PROPERTIES
    ),
    'required': ['type', 'id', 'bucket', 'region', 'access_key', 'secret_key', 'layout'],
    'additionalProperties': False,
}

SOURCES_SCHEMA = {
    'type': 'array',
    'items': {
        'oneOf': [
            # TODO: Implement HTTP sources
            S3_SOURCE_SCHEMA,
        ],
    }
}

IMAGE_STATUS_FIELDS = frozenset((
    'unwind_status',
    'debug_status'
))


class SymbolicationError(Exception):
    pass


class InvalidSourcesError(Exception):
    pass


class SymbolicatorSession(object):
    def __init__(self, url=None, sources=None, scope=None, timeout=None):
        self.url = url
        self.scope = scope
        self.sources = sources or []
        self.timeout = timeout
        self.session = None

    def __enter__(self):
        self.open()

    def __exit__(self, *args):
        self.close()

    def open(self):
        if self.session is None:
            self.session = Session()

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def _ensure_open(self):
        if not self.session:
            raise RuntimeError('Session not opened')

    def _request(self, method, url, **kwargs):
        self._ensure_open()

        url = urljoin(self.url, url)
        response = self.session.request(method, url, **kwargs)

        metrics.incr('events.symbolicator.status_code', tags={
            'status_code': response.status_code,
            # 'project_id': project_id,  # TODO
        })

        if method.lower() == 'get' and response.status_code == 404:
            return None

        response.raise_for_status()
        return response.json()

    def _get_task_params(self, timeout=None, scope=None):
        params = {}

        if timeout is None:
            timeout = self.timeout
        if timeout is not None:
            params['timeout'] = timeout

        if scope is None:
            scope = self.scope
        if scope is not None:
            params['scope'] = scope

        return params

    def symbolicate_stacktraces(self, stacktraces, modules, signal=None,
                                sources=None, timeout=None, scope=None):
        all_sources = self.sources
        all_sources += sources or []

        json = {
            'sources': all_sources,
            'stacktraces': stacktraces,
            'modules': modules,
        }

        if signal:
            json['signal'] = signal

        params = self._get_task_params(timeout=timeout, scope=scope)
        return self._request('post', 'symbolicate', params=params, json=json)

    def upload_minidump(self, minidump, sources=None, timeout=None, scope=None):
        files = {
            'upload_file_minidump': minidump
        }

        all_sources = self.sources
        all_sources += sources or []

        data = {
            'sources': json.dumps(all_sources),
        }

        params = self._get_task_params(timeout=timeout, scope=scope)
        return self._request('post', 'symbolicate', params=params, data=data, files=files)

    def query_task(self, task_id, timeout=None):
        task_url = 'requests/%s' % (task_id, )
        params = self._get_task_params(timeout=timeout)
        return self._request('get', task_url, params=params)

    def healthcheck(self):
        return self._request('get', 'healthcheck')


class Symbolicator(object):
    def __init__(self, url, sources=None, scope=None, timeout=None):
        self.url = url
        self.scope = scope
        self.sources = sources or []
        self.timeout = timeout

    def symbolicate_stacktraces(self, *args, **kwargs):
        with self.session() as session:
            return session.symbolicate_stacktraces(*args, **kwargs)

    def upload_minidump(self, *args, **kwargs):
        with self.session() as session:
            return session.upload_minidump(*args, **kwargs)

    def query_task(self, *args, **kwargs):
        with self.session() as session:
            return session.query_task(*args, **kwargs)

    def healthcheck(self, *args, **kwargs):
        with self.session() as session:
            return session.healthcheck(*args, **kwargs)

    def session(self):
        return SymbolicatorSession(
            url=self.url,
            scope=self.scope,
            sources=self.sources,
            timeout=self.timeout,
        )


def handles_frame(self, frame):
    if not frame:
        return False

    if get_path(frame, 'data', 'symbolication_status') is not None:
        return False

    platform = frame.get('platform') or self.data.get('platform')
    return platform in self.supported_platforms and 'instruction_addr' in frame


class SymbolicationTask(object):
    def __init__(self, symbolicator, project, data):
        self.symbolicator = symbolicator
        self.project = project
        self.data = data
        self.changed = False

    def symbolicate(self):
        raise NotImplementedError

    def result(self):
        if self.changed:
            return self.data



class NativeSymbolicationTask(SymbolicationTask):
    """
    Base class for all native symbolication tasks.
    """

    @memoize
    def _cache_key(self):
        return u'symbolicator-task-id:{}:{}:{}'.format(
            self.__class__.__name__,
            self.data['event_id'],
            self.data['project'],
        )

    def symbolicate(self):
        wait_timeout = 0
        current_attempt = 0

        with self.symbolicator.session() as session:
            while True:
                try:
                    response = self._do_symbolicate(session)
                    self.apply_response(response)
                except (RequestException, IOError):
                    # Any server error needs to be treated as a failure. We can
                    # retry a couple of times, but ultimately need to bail out.
                    current_attempt += 1
                    if current_attempt <= MAX_ATTEMPTS:
                        wait_timeout = 1.6 * wait_timeout + 0.5
                        time.sleep(wait_timeout)
                        continue

                    logger.error(
                        'Failed to contact symbolicator', exc_info=True)

                # Once we arrive here, we are done processing. Either, the
                # result is now ready, or we have exceeded MAX_ATTEMPTS. In both
                # cases, clean up the task id from the cache.
                default_cache.delete(self._cache_key)
                return

    def _do_symbolicate(self, session):
        task_id = default_cache.get(self._cache_key)
        response = None

        try:
            if task_id:
                # Processing has already started and we need to poll
                # symbolicator for an update. This in turn may put us back into
                # the queue.
                response = session.query_task(task_id)

            if response is None:
                # This is a new request, so we compute all request parameters
                # (potentially expensive if we need to pull minidumps), and then
                # upload all information to symbolicator. It will likely not
                # have a response ready immediately, so we start polling after
                # some timeout.
                response = self.call_symbolicator(session)
        except RequestException as e:
            # 503 can indicate that symbolicator is restarting. Wait for a
            # reboot, then try again. This overrides the default behavior of
            # retrying after just a second.
            if e.response.status_code == 503:
                raise RetrySymbolication(retry_after=10)

            raise

        # Symbolication is still in progress. Bail out and try again
        # after some timeout. Symbolicator keeps the response for the
        # first one to poll it.
        if response['status'] == 'pending':
            default_cache.set(
                self._cache_key,
                response['request_id'],
                REQUEST_CACHE_TIMEOUT)
            raise RetrySymbolication(retry_after=response['retry_after'])

        # TODO(ja): Figure out a way to avoid this
        default_cache.delete(self._cache_key)

        if response['status'] != 'completed':
            raise SymbolicationError(
                'Unexpected status: %s' % response['status'])

        return response

    def setdefault(self, path, value):
        if value is not None:
            if setdefault_path(self.data, *path, value=value):
                self.changed = True
        return get_path(self.data, *path)

    def apply_response(self, response):
        if response.get('crashed') is not None:
            self.setdefault(
                ['level'], 'fatal' if response['crashed'] else 'info')

        # We cannot extract exception codes or signals with the breakpad
        # extractor just yet. Once these capabilities are added to symbolic,
        # these values should go in the mechanism here.
        # TODO(ja): Check this

        self._apply_system_info(response.get('system_info') or {})
        self._apply_images(response.get('images') or [])

        self.apply_stacktraces(response.get('stacktraces') or [])

    def _apply_system_info(self, system_info):
        self.setdefault(['contexts', 'os', 'name'], system_info.get('os_name'))
        self.setdefault(['contexts', 'os', 'version'],
                        system_info.get('os_version'))
        self.setdefault(['contexts', 'os', 'build'],
                        system_info.get('os_build'))
        self.setdefault(['contexts', 'device', 'arch'],
                        system_info.get('cpu_arch'))

    def _apply_images(self, complete_images):
        raw_images = self.setdefault(['debug_meta', 'images'], [])

        for index, complete_image in enumerate(complete_images):
            raw_image = get_path(self.data, 'debug_meta',
                                 'images', index, default={})
            statuses = set()

            # Set image data from symbolicator as symbolicator might know more
            # than the SDK, especially for minidumps
            for k, v in six.iteritems(complete_image):
                if k in IMAGE_STATUS_FIELDS:
                    statuses.add(v)
                elif not (v is None or (k, v) == ('arch', 'unknown')):
                    raw_image[k] = v
                    self.changed = True

            for status in set(statuses):
                self._apply_image_status(status, raw_image)

            # TODO(ja): Doc this
            assert len(raw_images) >= index
            if len(raw_images) == index:
                raw_images.append(raw_image)

    def _apply_image_status(self, status, image):
        if status in ('found', 'unused'):
            return

        if status == 'missing':
            package = image.get('code_file')
            if not package or is_known_third_party(package):
                return

            if is_optional_package(package):
                ty = EventError.NATIVE_MISSING_OPTIONALLY_BUNDLED_DSYM
            else:
                ty = EventError.NATIVE_MISSING_DSYM
        elif status == 'malformed':
            ty = EventError.NATIVE_BAD_DSYM
        elif status == 'too_large':
            ty = EventError.FETCH_TOO_LARGE
        elif status == 'fetching_failed':
            ty = EventError.FETCH_GENERIC_ERROR
        elif status == 'other':
            ty = EventError.UNKNOWN_ERROR
        else:
            logger.error("Unknown status: %s", status)
            return

        self.add_error({
            'ty': ty,
            'message': None,  # TODO(ja): check this
            'image_arch': image.get('arch'),
            'image_path': image.get('code_file'),
            'image_name': image_name(image.get('code_file')),
            'image_uuid': image.get('debug_id'),
        })

    def add_error(self, error):
        error = {k: v for k, v in six.iteritems(error) if v is not None}
        self.data.setdefault('errors', []).append(error)
        self.changed = True

        if error['ty'] in FATAL_ERRORS and error['ty'] in USER_FIXABLE_ERRORS:
            report_processing_issue(
                self.data,
                scope='native',
                object=('dsym:%s' % error['image_uuid']) if error.get(
                    'image_uuid') else None,
                type=error['ty'],
                data=error
            )

    def map_frame(self, frame):
        frame = {
            'data': {
                'symbolication_status': frame['status'],
            },
            'instruction_addr': frame['instruction_addr'],
            'package': frame.get('package'),
            'symbol': frame.get('symbol'),
            'symbol_addr': frame.get('sym_addr'),
            'function': frame.get('function'),
            'filename': frame.get('filename'),
            'abs_path': frame.get('abs_path'),
            'lineno': frame.get('lineno'),
        }

        if frame['abs_path'] and not frame['filename']:
            frame['filename'] = posixpath.basename(frame['abs_path'])

        return {k: v for k, v in six.iteritems(frame) if k is not None}

    def call_symbolicator(self, session):
        raise NotImplementedError

    def apply_stacktraces(self, stacktraces):
        raise NotImplementedError



class PayloadSymbolicationTask(NativeSymbolicationTask):
    """
    Symbolicates a native event where stack traces are given in the event
    payload.
    """

    supported_platforms = ('cocoa', 'native')

    @memoize
    def stacktrace_infos(self):
        """
        List of stacktraces (with mixed frames) that should be symbolicated. An
        important property relied upon by callers is that the stacktrace lists
        themselves reference back into event data so they can be mutated in
        place.
        """
        return [
            stacktrace.stacktrace
            for stacktrace in find_stacktraces_in_data(self.data)
            if any(x in stacktrace.platforms for x in self.supported_platforms)
        ]

    @memoize
    def modules(self):
        return get_path(self.data, 'debug_meta', 'images', default=(), filter=True)

    @memoize
    def signal(self):
        return signal_from_data(self.data)

    def call_symbolicator(self, session):
        stacktraces = [
            [f for f in sinfo.stacktrace if handles_frame(f)]
            for sinfo in self.stacktrace_infos
        ]

        if not any(stacktraces):
            return {'status': 'completed'}

        return session.symbolicate_stacktraces(
            stacktraces=stacktraces,
            modules=self.modules,
            signal=self.signal
        )

    def apply_stacktraces(self, stacktraces):
        for sinfo, complete_stacktrace in zip(self.stacktrace_infos, stacktraces):
            complete_frames_by_idx = {}
            for complete_frame in complete_stacktrace['frames']:
                complete_frames_by_idx \
                    .setdefault(complete_frame['original_index'], []) \
                    .append(complete_frame)

            new_frames = []
            native_frames_idx = 0

            for raw_frame in sinfo.stacktrace['frames']:
                if handles_frame(raw_frame):
                    for complete_frame in complete_frames_by_idx[native_frames_idx]:
                        merged_frame = dict(raw_frame)
                        merged_frame.update(self.map_frame(complete_frame))
                        new_frames.append(merged_frame)
                    native_frames_idx += 1
                    self.changed = True
                else:
                    new_frames.append(raw_frame)

            if sinfo.container is not None and native_frames_idx > 0:
                sinfo.container['raw_stacktrace'] = dict(
                    sinfo.stacktrace,
                    frames=list(sinfo.stacktrace['frames'])
                )

            sinfo.stacktrace['frames'] = new_frames


class MinidumpSymbolicationTask(NativeSymbolicationTask):
    """
    Symbolicates a minidump and merges results into the event payload.
    """

    @memoize
    def minidump(self):
        cache_key = cache_key_for_event(self.data)
        attachments = attachment_cache.get(cache_key) or []
        return next((a for a in attachments if a.type == MINIDUMP_ATTACHMENT_TYPE), None)

    def call_symbolicator(self, session):
        minidump = self.minidump
        if not minidump:
            raise SymbolicationError('Missing minidump for minidump event')

        return session.upload_minidump(minidump.data)

    def apply_stacktraces(self, stacktraces):
        threads = [{
            'id': thread.get('thread_id'),
            'crashed': thread.get('is_requesting'),
            'stacktrace': {
                'frames': [self.map_frame(f) for f in thread['frames']],
                'registers': thread.get('registers'),
            }
        } for thread in stacktraces]

        self.data['threads'] = {
            'values': threads
        }


class UnrealSymbolicationTask(SymbolicationTask):
    """
    Symbolicates a UE4 crash report.
    """

    @property
    def get_threads(self):
        return get_path(self.data, 'threads', 'values') or get_path(self.data, 'threads') or ()

    def _apply_minidump(self):
        minidump_task = MinidumpSymbolicationTask(self.symbolicator, self.project, self.data)
        minidump_task.symbolicate()
        self.data = minidump_task.result() or self.data
        self.changed = self.changed or minidump_task.changed


    def _apply_portable_callstack(self):
        if any(thread.get('stacktrace') and thread.get('crashed')
               for thread in self.get_threads()):
            return

        portable_callstack = get_path(self.data, 'contexts', 'unreal',
                                      'portable_call_stack')
        if portable_callstack is None:
            return

        images = get_path(self.data, 'debug_meta', 'images', filter=True, default=())
        frames = parse_portable_callstack(portable_callstack, images)

        if not frames:
            return

        unreal_tmp_event = {
            'debug_meta': {'images': images},
            'threads': {
                'values': [
                    {
                        'stacktrace': {'frames': frames},
                        'crashed': True,
                    }
                ]
            },
            'platform': 'native',
            'project': self.data['project'],
            'event_id': self.data['event_id'],
        }

        portable_callstack_task = PayloadSymbolicationTask(
            self.symbolicator,
            self.project,
            unreal_tmp_event
        )

        portable_callstack_task.symbolicate()
        unreal_tmp_event = portable_callstack_task.result() or unreal_tmp_event
        self.get_threads().extend(unreal_tmp_event['threads']['values'])
        self.changed = bool(unreal_tmp_event['threads']['values'])

    @memoize
    def _state_cache_key(self):
        return u'symbolicator-unreal-state:{}:{}'.format(
            self.data['event_id'],
            self.data['project']
        )

    def symbolicate(self):
        # Build a state machine persisted in Redis so we know where to resume
        # in case of `RetrySymbolication`. Otherwise we will e.g. attempt to
        # process the minidump twice, should processing the portable callstack
        # throw `RetrySymbolication`.
        #
        # None is both the beginning and the end state, but that's unavoidable
        # considering we don't want to persist the state in Redis forever.
        #
        # None -> PROCESS_MINIDUMP -> PROCESS_PORTABLE_CALLSTACK -> None
        PROCESS_MINIDUMP = 1
        PROCESS_PORTABLE_CALLSTACK = 2

        TRANSITIONS = {
            PROCESS_MINIDUMP: (self._apply_minidump, PROCESS_PORTABLE_CALLSTACK),
            PROCESS_PORTABLE_CALLSTACK: (self._apply_portable_callstack, None)
        }

        state = default_cache.get(self._state_cache_key)
        if state is None:
            state = PROCESS_MINIDUMP

        original_state = state

        try:
            while state is not None:
                func, new_state = TRANSITIONS[state]
                func()
                state = new_state

        except RetrySymbolication as e:
            e.new_data = self.data
            raise e
        finally:
            if state != original_state:
                if state is None:
                    default_cache.delete(self._state_cache_key)
                else:
                    default_cache.set(self._state_cache_key, state)


# TODO(ja): Move to minidump.py
def is_minidump_event(data):
    context = get_path(data, 'contexts', 'minidump')
    return bool(context)


# TODO(ja): Move to unreal.py
def is_unreal_event(data):
    context = get_path(data, 'contexts', 'unreal')
    return bool(context)


def get_internal_source(project):
    """
    Returns the source configuration for a Sentry project.
    """
    internal_url_prefix = options.get('system.internal-url-prefix')
    if not internal_url_prefix:
        internal_url_prefix = options.get('system.url-prefix')
        if sys.platform == 'darwin':
            internal_url_prefix = internal_url_prefix \
                .replace("localhost", "host.docker.internal") \
                .replace("127.0.0.1", "host.docker.internal")

    assert internal_url_prefix
    sentry_source_url = '%s%s' % (
        internal_url_prefix.rstrip('/'),
        reverse('sentry-api-0-dsym-files', kwargs={
            'organization_slug': project.organization.slug,
            'project_slug': project.slug
        })
    )

    return {
        'type': 'sentry',
        'id': 'sentry:project',
        'url': sentry_source_url,
        'token': get_system_token(),
    }


def parse_sources(config):
    """
    Parses the given sources in the config string (from JSON).
    """

    if not config:
        return []

    try:
        sources = json.loads(config)
    except BaseException as e:
        raise InvalidSourcesError(e.message)

    try:
        jsonschema.validate(sources, SOURCES_SCHEMA)
    except jsonschema.ValidationError as e:
        raise InvalidSourcesError(e.message)

    ids = set()
    for source in sources:
        if source['id'].startswith('sentry'):
            raise InvalidSourcesError(
                'Source ids must not start with "sentry:"')
        if source['id'] in ids:
            raise InvalidSourcesError(
                'Duplicate source id: %s' % (source['id'], ))
        ids.add(source['id'])

    return sources


def get_sources_for_project(project):
    """
    Returns a list of symbol sources for this project.
    """

    sources = []

    # The symbolicator evaluates sources in the order they are declared. Always
    # try to download symbols from Sentry first.
    project_source = get_internal_source(project)
    sources.append(project_source)

    sources_config = project.get_option('sentry:symbol_sources')
    if sources_config:
        try:
            custom_sources = parse_sources(sources_config)
            sources.extend(custom_sources)
        except InvalidSourcesError:
            # Source configs should be validated when they are saved. If this
            # did not happen, this indicates a bug. Record this, but do not stop
            # processing at this point.
            logger.error('Invalid symbolicator source config', exc_info=True)

    # Add builtin sources last to ensure that custom sources have precedence
    # over our defaults.
    builtin_sources = project.get_option('sentry:builtin_symbol_sources') or []
    for key, source in six.iteritems(BUILTIN_SOURCES):
        if key in builtin_sources:
            sources.append(source)

    return sources


def create_symbolicator(project):
    opts = options.get('symbolicator.options') or {}
    if opts.get('timeout') is None:
        opts['timeout'] = SYMBOLICATOR_TIMEOUT

    opts['scope'] = six.text_type(project.id)
    opts['sources'] = get_sources_for_project(project)

    return Symbolicator(**opts)


def should_use_symbolicator(project):
    return options.get('symbolicator.enabled') and \
        project.get_option('sentry:symbolicator-enabled')


def symbolicate_native_event(data):
    project = Project.objects.get_from_cache(id=data['project'])

    # Compatibility shim with old pipeline: Execute minidump CFI processing
    # directly here. Symbolicator applies CFI, so this needs to be removed once
    # all project have been switched.
    # TODO(ja): Remove this
    if not should_use_symbolicator(project):
        return reprocess_minidump_with_cfi(data) if is_minidump_event(data) else None

    # TODO: Check for unreal portable callstack
    if is_unreal_event(data):
        task_cls = UnrealSymbolicationTask
    elif is_minidump_event(data):
        task_cls = MinidumpSymbolicationTask
    else:
        task_cls = PayloadSymbolicationTask

    symbolicator = create_symbolicator(project)
    task = task_cls(symbolicator, project, data)
    task.symbolicate()
    return task.result()