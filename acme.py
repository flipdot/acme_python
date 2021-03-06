#!/usr/bin/python2
import time

import logging
import os
import re
import ssl
import subprocess
import threading

from . import config

acme_sh = [os.path.dirname(__file__) + "/acme.sh/acme.sh"]

logger = logging.getLogger(__name__)


class ACME(object):
    def __init__(self, app, staging=True):
        logger.info("preparing ACME for %s", config.ACME_DOMAIN)
        if staging:
            if "--staging" not in acme_sh:
                acme_sh.append("--staging")
        else:
            if "--staging" in acme_sh:
                acme_sh.remove("--staging")

        self.account_thumb = self.get_account()
        self.https_srv = None
        self.app = app
        app.route('/.well-known/acme-challenge/<challenge>')(
            self.handle_challenge)

        self.context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        self.https_thread = None
        self.try_load_cert()
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.do_run = True
        self.cond = threading.Condition()
        self.thread.start()

    def stop(self):
        self.do_run = False
        self.cond.acquire()
        self.cond.notify_all()
        self.cond.release()

    @staticmethod
    def cert_paths():
        base_path = "%s/.acme.sh/%s" % (os.getenv("HOME"), config.ACME_DOMAIN)
        cert = "%s/fullchain.cer" % base_path
        key = "%s/%s.key" % (base_path, config.ACME_DOMAIN)
        return cert, key

    def try_load_cert(self):
        cert, key = self.cert_paths()
        logger.debug("Cert: %s; %s", cert, key)
        try:
            self.context.load_cert_chain(cert, key)
            self.start_https()
        except IOError as e:
            logger.warning("No cert file (yet): %s %s", repr(e), e.filename)

    def run(self):
        # wait for flask to start up
        time.sleep(3)
        while self.do_run:
            self.issue_cert()

            self.cond.acquire()
            self.cond.wait(60 * 60)
            self.cond.release()

    def start_https(self):
        if not self.https_thread:
            self.https_thread = threading.Thread(target=self.run_https)
        if not self.https_thread.is_alive():
            try:
                self.https_thread.start()
            except RuntimeError as e:
                if "started once" in e:
                    logger.info("Restarting https...")
                    self.https_srv.shutdown()
                    self.https_thread.join()
                    self.https_thread = None
                    self.start_https()

    def run_https(self):
        from werkzeug.serving import make_server
        self.https_srv = make_server("0.0.0.0", config.HTTPS_PORT, self.app,
                                     threaded=True, processes=0, passthrough_errors=True,
                                     ssl_context=self.context)
        logger.info("Running at https://%s:%d/", config.ACME_DOMAIN, config.HTTPS_PORT)
        self.https_srv.serve_forever()

    def handle_challenge(self, challenge):
        full = challenge + "." + self.account_thumb
        return full

    @staticmethod
    def get_account():
        try:
            out = sh(acme_sh + ["--register-account", "--accountemail", config.ACME_EMAIL])
        except ACMEError as e:
            e.message = "Registering account. " + e.message
            raise e
        logger.debug("register account: %s", out)
        for match in re.finditer(b"ACCOUNT_THUMBPRINT='([^']+)'", out):
            c = match.group(1)
            logger.debug("Challenge: %s", c)
            return c
        raise ACMEError("no thumprint found. output was: %s", out)

    def issue_cert(self):
        try:
            out = sh(acme_sh + ["--renew", "-d", config.ACME_DOMAIN])
        except ACMEError as e:
            if b"Skip, Next renewal time is" in e.output:
                logger.info("Cert is up-to date, renewal: %s",
                            e.output.split("renewal time is: ")[1])
                return
            raise e
        if "not a issued domain" in out:
            out = sh(acme_sh + ["--issue", "--stateless", "-d", config.ACME_DOMAIN])
            logger.info("issued cert: %s", out)
            self.try_load_cert()
            return
        # renewed cert TODO check output
        logger.info("renewed cert: %s", out)
        self.try_load_cert()


def sh(argv):
    try:
        out = subprocess.check_output(argv, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        raise ACMEError("exec %s [%d]: %s" % (argv, e.returncode, e.output), e)
    return out


class ACMEError(BaseException):
    def __init__(self, msg, base=None):
        super(ACMEError, self).__init__(msg, base)
        if isinstance(base, subprocess.CalledProcessError):
            self.returncode = base.returncode
            self.output = base.output
        else:
            self.output = None
            self.returncode = None
