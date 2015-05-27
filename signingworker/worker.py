import os
import random
import shutil
import tempfile
import urlparse
import arrow
from jsonschema import ValidationError
from kombu import Exchange, Queue
from kombu.mixins import ConsumerMixin
import redo
import logging
import requests
import sh
import json
import taskcluster

from signingworker.task import validate_task
from signingworker.exceptions import TaskVerificationError, \
    ChecksumMismatchError, SigningServerError
from signingworker.utils import my_ip, get_hash

log = logging.getLogger(__name__)


class SigningConsumer(ConsumerMixin):

    def __init__(self, connection, exchange, queue_name, worker_type,
                 taskcluster_config, signing_servers, signing_server_auth,
                 supported_signing_scopes, tools_checkout):
        self.connection = connection
        self.exchange = Exchange(exchange, type='topic', passive=True)
        self.queue_name = queue_name
        self.worker_type = worker_type
        self.routing_key = "*.*.*.*.*.*.{}.#".format(self.worker_type)
        self.tc_queue = taskcluster.Queue(taskcluster_config)
        self.signing_servers = signing_servers
        # TODO: Explicitly set formats in manifest
        self.signing_formats = ["mar", "gpg"]
        self.signing_server_auth = signing_server_auth
        self.supported_signing_scopes = supported_signing_scopes
        self.tools_checkout = tools_checkout
        self.cert = os.path.join(self.tools_checkout,
                                 "release/signing/host.cert")

    def get_consumers(self, consumer_cls, channel):
        queue = Queue(name=self.queue_name, exchange=self.exchange,
                      routing_key=self.routing_key, durable=True,
                      exclusive=False, auto_delete=False)
        return [consumer_cls(queues=[queue], callbacks=[self.process_message])]

    def process_message(self, body, message):
        task_id = None
        run_id = None
        try:
            task_id = body["status"]["taskId"]
            run_id = body["status"]["runs"][-1]["runId"]
            log.debug("Claiming task %s, run %s", task_id, run_id)
            self.tc_queue.claimTask(
                task_id, run_id,
                {"workerGroup": self.worker_type, "workerId": self.worker_type}
            )
            task = self.tc_queue.task(task_id)
            task_graph_id = task["taskGroupId"]
            validate_task(task, self.supported_signing_scopes)
            self.sign(task_id, run_id, task)
            log.debug("Completing: %s, r: %s", task_id, run_id)
            self.tc_queue.reportCompleted(task_id, run_id)
            log.debug("Complete: %s, r: %s, tg: %s", task_id, run_id,
                      task_graph_id)
        except taskcluster.exceptions.TaskclusterRestFailure as e:
            log.exception("TC REST failure, %s", e.status_code)
            if e.status_code == 409:
                log.debug("Task already claimed, acking...")
            else:
                raise
        except (TaskVerificationError, ValidationError):
            log.exception("Cannot verify task, %s", body)
            self.tc_queue.reportException(
                task_id, run_id, {"reason": "malformed-payload"})
        except Exception:
            log.exception("Error processing %s", body)

        message.ack()

    @redo.retriable(attempts=10, sleeptime=5, max_sleeptime=30)
    def get_manifest(self, url):
        r = requests.get(url)
        r.raise_for_status()
        return r.json()

    def sign(self, task_id, run_id, task):
        payload = task["payload"]
        manifest_url = payload["signingManifest"]
        signing_manifest = self.get_manifest(manifest_url)
        # TODO: better way to extract filename
        url_prefix = "/".join(manifest_url.split("/")[:-1])
        for e in signing_manifest:
            # TODO: "mar" is too specific, change the manifest
            file_url = "{}/{}".format(url_prefix, e["mar"])
            abs_filename = self.download_and_sign_file(
                task_id, run_id, file_url, e["hash"])
            # Update manifest data with new values
            e["hash"] = get_hash(abs_filename)
            e["size"] = os.path.getsize(abs_filename)
        _, manifest_file = tempfile.mkstemp()
        with open(manifest_file, "wb") as f:
            json.dump(signing_manifest, f, indent=2, sort_keys=True)
        log.debug("Uploading manifest for t: %s, r: %s", task_id, run_id)
        self.create_artifact(task_id, run_id, "public/env/manifest.json",
                             manifest_file, "application/json")

    def download_and_sign_file(self, task_id, run_id, url, checksum):
        work_dir = tempfile.mkdtemp()
        # TODO: better parsing
        filename = urlparse.urlsplit(url).path.split("/")[-1]
        abs_filename = os.path.join(work_dir, filename)
        r = requests.get(url)
        r.raise_for_status()
        with open(abs_filename, 'wb') as fd:
            for chunk in r.iter_content(4096):
                fd.write(chunk)
        got_checksum = get_hash(abs_filename)
        if not got_checksum == checksum:
            log.debug("Checksum mismatch, cleaning up...")
            shutil.rmtree(work_dir)
            raise ChecksumMismatchError("Expected {}, got {} for {}".format(
                checksum, got_checksum, url
            ))
        self.sign_file(work_dir, filename)
        self.create_artifact(task_id, run_id, "public/env/%s" % filename,
                             abs_filename)
        return abs_filename

    def create_artifact(self, task_id, run_id, dest, abs_filename,
                        content_type="application/octet-stream"):
        log.debug("Uploading artifact %s (t: %s, r: %s) from %s (%s)", dest,
                  task_id, run_id, abs_filename, content_type)
        # TODO: better expires
        res = self.tc_queue.createArtifact(
            task_id, run_id, dest,
            {
                "storageType": "s3",
                "contentType": content_type,
                "expires": arrow.now().replace(weeks=2).isoformat()
            }
        )
        log.debug("Got %s", res)
        put_url = res["putUrl"]
        log.debug("Uploading to %s", put_url)
        taskcluster.utils.putFile(abs_filename, put_url, content_type)

    @redo.retriable(attempts=10, sleeptime=5, max_sleeptime=30)
    def get_token(self, output_file):
        token = None
        data = {"slave_ip": my_ip, "duration": 5 * 60}
        random.shuffle(self.signing_servers)
        for server in self.signing_servers:
            log.debug("getting token from %s", server)
            # TODO: Figure out how to deal with certs not matching hostname,
            #  error: https://gist.github.com/rail/cbacf2d297decb68affa
            r = requests.post("https://{}/token".format(server), data=data,
                              auth=tuple(self.signing_server_auth),
                              verify=False)
            r.raise_for_status()
            if r.content:
                token = r.content
                break
        if not token:
            raise SigningServerError("Cannot retrieve signing token")
        with open(output_file, "wb") as f:
            f.write(token)

    def sign_file(self, work_dir, from_, to=None):
        if to is None:
            to = from_
        token = os.path.join(work_dir, "token")
        nonce = os.path.join(work_dir, "nonce")
        self.get_token(token)
        signtool = os.path.join(self.tools_checkout,
                                "release/signing/signtool.py")
        cmd = [signtool, "-n", nonce, "-t", token, "-c", self.cert]
        for s in self.signing_servers:
            cmd.extend(["-H", s])
        for f in self.signing_formats:
            cmd.extend(["-f", f])
        cmd.extend(["-o", to, from_])
        sh.python(*cmd, _err_to_out=True, _cwd=work_dir)
