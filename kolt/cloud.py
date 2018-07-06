"""
This modules contains some helper functions to inject cloud-init
to booted machines. At the moment only Cloud Inits for Ubunut 16.04 are
provided
"""
import base64
import datetime
import json
import logging
import os
import textwrap
import subprocess as sp
import sys


from pkg_resources import (Requirement, resource_filename)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from kolt.ssl import create_key, create_certificate

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
# add ch to logger
logger.addHandler(ch)


INCLUSION_TYPES_MAP = {
    '#include': 'text/x-include-url',
    '#include-once': 'text/x-include-once-url',
    '#!': 'text/x-shellscript',
    '#cloud-config': 'text/cloud-config',
    '#upstart-job': 'text/upstart-job',
    '#part-handler': 'text/part-handler',
    '#cloud-boothook': 'text/cloud-boothook',
    '#cloud-config-archive': 'text/cloud-config-archive',
    '#cloud-config-jsonp': 'text/cloud-config-jsonp',
}


class CloudInit:

    def __init__(self, role, hostname, cluster_info, os_type='ubuntu',
                 os_version="16.04"):
        """
        cluster_info - a dictionary with infromation about the etcd cluster
        members
        """
        self.combined_message = MIMEMultipart()

        if role not in ('master', 'node'):
            raise ValueError("Incorrect os_role!")

        self.role = role
        self.hostname = hostname
        self.cluster_info = cluster_info
        self.os_type = os_type
        self.os_version = os_version

    def _etcd_cluster_info(self):
        """
        Write the etcd cluster info to /etc/kolt.conf
        """
        ca_key = create_key()
        ca_cert = create_certificate(ca_key, ca_key.public_key(),
                                     "DE", "BY", "NUE",
                                     "noris-network", "CA", ["CA"])

        hostnames = [v for k, v in self.cluster_info.items() if v.endswith("_name")]

        k8s_key = create_key()
        k8s_cert = create_certificate(ca_key, k8s_key.public_key(),
                                     "DE", "BY", "NUE", "noris-network",
                                     "Kubernetes", hostnames)

        cluster_info_part = """
        #cloud-config
        write_files:
            - content: |
                NODE01={n01_name}
                NODE02={n02_name}
                NODE03={n03_name}
                NODE01_IP={n01_ip}
                NODE02_IP={n02_ip}
                NODE03_IP={n03_ip}

              owner: root:root
              permissions: '0644'
              path: /etc/kolt.conf
        """.format(**self.cluster_info)
        return textwrap.dedent(cluster_info_part)

    def _get_certificate_info(self):
        """
        write certificates to destination directory
        """
        certificate_info = """
        #cloud-config
        write_files:
            - path: /etc/ssl/ca.pem
              encoding: b64
              content: {CA_CERT}
              owner: root:root
              permissions: '0600'
            - path: /etc/ssl/{HOST_CERT_NAME}
              encoding: b64
              content: {HOST_CERT}
              owner: root:root
              permissions: '0600'
            - path: /etc/ssl/{HOST_KEY_NAME}
              encoding: b64
              content: {HOST_KEY}
              owner: root:root
              permissions: '0600'
        """.format(
            CA_CERT=base64.b64encode(
                open("./ca.pem", "rb").read()).decode(),
            HOST_CERT=base64.b64encode(
                open("./" + self.hostname + ".pem", "rb").read()).decode(),
            HOST_CERT_NAME=self.hostname + ".pem",
            HOST_KEY=base64.b64encode(
                open("./" + self.hostname + "-key.pem", "rb").read()).decode(),
            HOST_KEY_NAME=self.hostname + "-key.pem"
        )
        ret = textwrap.dedent(certificate_info)
        print(ret)
        return ret

    def __str__(self):

        if self.cluster_info:
            sub_message = MIMEText(
                self._etcd_cluster_info(),
                _subtype='text/cloud-config')
            sub_message.add_header('Content-Disposition', 'attachment',
                                   filename="/etc/kolt.conf")
            self.combined_message.attach(sub_message)

        #sub_message = MIMEText(self._get_certificate_info(),
        #                       _subtype='text/cloud-config')
        #sub_message.add_header('Content-Disposition', 'attachment',
        #                       filename="/etc/cert.conf")
        #self.combined_message.attach(sub_message)

        k8s_bootstrap = "bootstrap-k8s-%s-%s-%s.sh" % (self.role,
                                                       self.os_type,
                                                       self.os_version)

        # process bootstrap script and generic cloud-init file
        for item in ['generic', k8s_bootstrap]:
            fh = open(resource_filename(Requirement('kolt'),
                                        os.path.join('kolt',
                                                     'cloud-inits',
                                                     item)))
            # we currently blindly assume the first line is a mimetype
            # or a shebang
            main_type, _subtype = fh.readline().strip().split("/", 1)

            if '#!' in main_type:
                _subtype = 'x-shellscript'
            #    fh.seek(0)

            sub_message = MIMEText(fh.read(), _subtype=_subtype)
            sub_message.add_header('Content-Disposition',
                                   'attachment', filename="%s" % item)
            self.combined_message.attach(sub_message)
            fh.close()

        return self.combined_message.as_string()
