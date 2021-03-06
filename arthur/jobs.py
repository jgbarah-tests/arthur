# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#     Alvaro del Castillo San Felix <acs@bitergia.com>
#

import functools
import logging

import rq
import pickle

import perceval
import perceval.backends
import perceval.cache

from grimoirelab.toolkit.datetime import unixtime_to_datetime
from grimoirelab.toolkit.introspect import find_signature_parameters

from ._version import __version__
from .common import MAX_JOB_RETRIES
from .errors import NotFoundError


logger = logging.getLogger(__name__)


def metadata(func):
    """Add metadata to an item.

    Decorator that adds metadata to Perceval items such as the
    identifier of the job that generated it or the version of
    the system. The contents from the original item will
    be stored under the 'data' keyword.

    Take into account that this function only can be called from
    a `PercevalJob` class due it needs access to some attributes
    and methods of this class.
    """
    @functools.wraps(func)
    def decorator(self, *args, **kwargs):
        for item in func(self, *args, **kwargs):
            item['arthur_version'] = __version__
            item['job_id'] = self.job_id
            yield item
    return decorator


class JobResult:
    """Class to store the result of a Perceval job.

    It stores useful data such as the taks_id, the UUID of the last
    item generated or the number of items fetched by the backend.

    :param job_id: job identifier
    :param task_id: identitifer of the task linked to this job
    :param backend: backend used to fetch the items
    :param last_uuid: UUID of the last item
    :param max_date: maximum date fetched among items
    :param nitems: number of items fetched by the backend
    :param offset: maximum offset fetched among items
    :param nresumed: number of time the job was resumed
    """
    def __init__(self, job_id, task_id, backend, last_uuid,
                 max_date, nitems, offset=None, nresumed=0):
        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.last_uuid = last_uuid
        self.max_date = max_date
        self.nitems = nitems
        self.offset = offset
        self.nresumed = nresumed


class PercevalJob:
    """Class for wrapping Perceval jobs.

    Wrapper for running and executing Perceval backends. The items
    generated by the execution of a backend will be stored on the
    Redis queue named `qitems`. The result of the job can be obtained
    accesing to the property `result` of this object.

    :param job_id: job identifier
    :param task_id: identitifer of the task linked to this job
    :param backend: name of the backend to execute
    :param conn: connection with a Redis database
    :param qitems: name of the queue where items will be stored

    :rasises NotFoundError: raised when the backend is not avaliable
        in Perceval
    """
    def __init__(self, job_id, task_id, backend, conn, qitems):
        try:
            self._bklass = perceval.find_backends(perceval.backends)[0][backend]
        except KeyError:
            raise NotFoundError(element=backend)

        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.conn = conn
        self.qitems = qitems
        self.retries = 0
        self.cache = None
        self._result = JobResult(self.job_id, self.task_id, self.backend,
                                 None, None, 0, offset=None,
                                 nresumed=0)

    @property
    def result(self):
        return self._result

    def run(self, backend_args, resume=False, fetch_from_cache=False):
        """Run the backend with the given parameters.

        The method will run the backend assigned to this job,
        storing the fetched items in a Redis queue. The ongoing
        status of the job, can be accessed through the property
        `result`. When `resume` is set, the job will start from
        the last execution, overewritting 'from_date' and 'offset'
        parameters, if needed.

        Setting to `True` the parameter `fetch_from_cache`, items can
        be fetched from the cache assigned to this job.

        Any exception during the execution of the process will
        be raised.

        :param backend_args: parameters used to un the backend
        :param fetch_from_cache: fetch items from the cache
        :param resume: fetch items starting where the last
            execution stopped
        """
        args = backend_args.copy()
        args['cache'] = self.cache

        if not resume:
            self._result = JobResult(self.job_id, self.task_id, self.backend,
                                     None, None, 0, offset=None,
                                     nresumed=0)
        else:
            if self.result.max_date:
                args['from_date'] = unixtime_to_datetime(self.result.max_date)
            if self.result.offset:
                args['offset'] = self.result.offset
            self._result.nresumed += 1

        for item in self._execute(args, fetch_from_cache):
            self.conn.rpush(self.qitems, pickle.dumps(item))

            self._result.nitems += 1
            self._result.last_uuid = item['uuid']

            if not self.result.max_date or self.result.max_date < item['updated_on']:
                self._result.max_date = item['updated_on']
            if 'offset' in item:
                self._result.offset = item['offset']

    def initialize_cache(self, dirpath, backup=False):
        """Initializes the cache of this job.

        The method initializes the cache object related to this job
        storing its data under `dirpath`. When `backup` is set, the
        cache will keep a copy of the data for restoring.

        :param dirpath: path to the cache data
        :param backup: keep a copy of the cache data

        :raises ValueError: when dirpath is empty
        """
        if not dirpath:
            raise ValueError("dirpath requieres a value")

        logger.debug("Initializing cache of job %s on path '%s' completed",
                     self.job_id, dirpath)

        self.cache = perceval.cache.Cache(dirpath)

        if backup:
            self.cache.backup()
            logger.debug("Cache backup of job %s on path '%s' completed",
                         self.job_id, dirpath)

        logger.debug("Cache on '%s' initialized", dirpath)

    def recover_cache(self):
        """Restore the backup from the job's cache.

        When the cache assigned to this job has a backup, this method
        will restore it. Otherwise, it will do nothing.
        """
        if not self.cache:
            return

        self.cache.recover()

        logger.debug("Cache of job %s on path '%s' recovered",
                     self.job_id, self.cache.cache_path)

    def has_caching(self):
        """Returns if the job supports items caching"""

        return self._bklass.has_caching()

    def has_resuming(self):
        """Returns if the job can be resumed when it fails"""

        return self._bklass.has_resuming()

    @metadata
    def _execute(self, backend_args, fetch_from_cache):
        """Execute a backend of Perceval.

        Run the backend of Perceval assigned to this job using the
        given arguments. It will raise an `AttributeError` when any of
        the required parameters to run the backend are not found.
        Other exceptions related to the execution of the backend
        will be raised too.

        This method will return an iterator of the items fetched
        by the backend. These items will include some metadata
        related to this job.

        It will also be possible to retrieve the items from the
        cache setting to `True` the parameter `fetch_from_cache`.

        :param bakend_args: arguments to execute the backend
        :param fetch_from_cache: retieve items from the cache

        :returns: iterator of items fetched by the backend

        :raises AttributeError: raised when any of the required
            parameters is not found
        """
        kinit = find_signature_parameters(self._bklass.__init__, backend_args)
        obj = self._bklass(**kinit)

        if not fetch_from_cache:
            fnc_fetch = obj.fetch
        else:
            fnc_fetch = obj.fetch_from_cache

        kfetch = find_signature_parameters(fnc_fetch, backend_args)

        for item in fnc_fetch(**kfetch):
            yield item


def execute_perceval_job(backend, backend_args, qitems, task_id,
                         cache_path=None, fetch_from_cache=False,
                         max_retries=MAX_JOB_RETRIES):
    """Execute a Perceval job on RQ.

    The items fetched during the process will be stored in a
    Redis queue named `queue`.

    Setting the parameter `cache_path`, raw data will be stored
    in the cache. The contents from the cache can be retrieved
    setting the pameter `fetch_from_cache` to `True`, too. Take into
    account this behaviour will be only available when the backend
    supports the use of the cache. If caching is not supported, an
    `AttributeErrror` exception will be raised.

    :param backend: backend to execute
    :param bakend_args: dict of arguments for running the backend
    :param qitems: name of the RQ queue used to store the items
    :param task_id: identifier of the task linked to this job
    :param cache_path: path to the cache
    :param fetch_from_cache: retrieve items from the cache
    :param max_retries: maximum number of retries if a job fails

    :returns: a `JobResult` instance

    :raises NotFoundError: raised when the backend is not found
    :raises AttributeError: raised when caching is not supported but
        any of the cache parameters were set
    """
    rq_job = rq.get_current_job()

    job = PercevalJob(rq_job.id, task_id, backend,
                      rq_job.connection, qitems)

    logger.debug("Running job #%s (task: %s) (%s)",
                 job.job_id, task_id, backend)

    if not job.has_caching() and (cache_path or fetch_from_cache):
        raise AttributeError("cache attributes set but cache is not supported")

    if cache_path:
        job.initialize_cache(cache_path, not fetch_from_cache)

    run_job = True
    resume = False
    failures = 0

    while run_job:
        try:
            job.run(backend_args, resume=resume,
                    fetch_from_cache=fetch_from_cache)
        except AttributeError as e:
            raise e
        except Exception as e:
            logger.debug("Error running job %s (%s) - %s",
                         job.job_id, backend, str(e))
            failures += 1

            if cache_path and not fetch_from_cache:
                job.recover_cache()
                job.cache = None

            if not job.has_resuming() or failures >= max_retries:
                logger.error("Cancelling job #%s (task: %s) (%s)",
                             job.job_id, task_id, backend)
                raise e

            logger.warning("Resuming job #%s (task: %s) (%s) due to a failure (n %s, max %s)",
                           job.job_id, task_id, backend, failures, max_retries)
            resume = True
        else:
            # No failure, do not retry
            run_job = False

    result = job.result

    logger.debug("Job #%s (task: %s) completed (%s) - %s items fetched",
                 result.job_id, task_id, result.backend, str(result.nitems))

    return result
