"""
This modules contains some helper functions to inject cloud-init
to booted machines. At the moment only Cloud Inits for Ubunut 16.04 are
provided
"""
import base64
import os
import yaml

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pkg_resources import (Requirement, resource_filename, get_distribution)
from datetime import datetime

from cryptography.hazmat.primitives import serialization

from koris.ssl import b64_cert, b64_key
from koris.util.util import get_logger

LOGGER = get_logger(__name__)


BOOTSTRAP_SCRIPTS_DIR = "/koris/provision/userdata/"


class BaseInit:
    """
    Attributes:
        cloud_config_data       this attribute contains the text/cloud-config
                                files that is passed to the instances
        attachments             this attribute contains other parts of the
                                userdata, e.g scripts that are directly
                                executed by cloud-init. They should be
                                instances of MIMEText with the header
                                'Content-Disposition' set to 'attachment'
    """
    def __init__(self):
        self._cloud_config_data = {}
        self._attachments = []

        # if needed we can declare and use other sections at this point...
        self._cloud_config_data['write_files'] = []
        self._cloud_config_data['ssh_authorized_keys'] = []

        # TODO: Do we still need this?
        self._cloud_config_data['manage_etc_hosts'] = True
        self._cloud_config_data['runcmd'] = []
        self._cloud_config_data['runcmd'].append('swapoff -a')

        # assemble the parts
        self._write_koris_info()

    def write_file(self, path, content, owner="root", group="root",
                   permissions="0600", encoder=lambda x: base64.b64encode(x)):
        """
        writes a file to the instance
        path: e.g. /etc/kubernetes/koris.conf
        content: string of the content of the file
        owner: e.g. root
        group: e.g. root
        permissions: e.g. "0644", as string
        encode: Optional encoder to use for the needed base64 encoding
        """
        data = {
            "path": path,
            "owner": owner+":"+group,
            "encoding": "b64",
            "permissions": permissions,
            "content": encoder(content.encode()).decode()
        }
        self._cloud_config_data['write_files'].append(data)

    def execute_shell_script(self, script):
        """
        execute a script on the booted instance.
        script: the script (str) to execute after provisioning the instance
        """
        part = MIMEText(script, _subtype='x-shellscript')
        part.add_header('Content-Disposition', 'attachment')
        self._attachments.append(part)

    def add_ssh_public_key(self, ssh_key):
        """
        ssh_key should be the key pair to use of type:
            cryptography.hazmat.backends.openssl.rsa._RSAPrivateKey
        which is the return value of function koris.ssl.create_key
        """
        keyline = ssh_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH).decode()
        self._cloud_config_data['ssh_authorized_keys'].append(keyline)

    def _write_koris_info(self):
        """
        Generate the koris.conf configuration file.
        """
        content = """# This file contains meta information about koris

        koris_version={}
        creation_date={}
        """.format(
            get_distribution('koris').version,
            datetime.strftime(datetime.now(), format="%c"))

        self.write_file("/etc/kubernetes/koris.conf", content, "root", "root",
                        "0644")

    def __str__(self):
        """
        This method generates a string from the cloud_config_data and the
        attachments that have been set in the corresponding attributes.
        """
        userdata = MIMEMultipart()

        # first add the cloud-config-data script
        config = MIMEText(yaml.dump(self._cloud_config_data),
                          _subtype='cloud-config')
        config.add_header('Content-Disposition', 'attachment')
        userdata.attach(config)

        for attachment in self._attachments:
            userdata.attach(attachment)

        return userdata.as_string()


class NthMasterInit(BaseInit):
    """
    Initialization userdata for an n-th master node. Nothing more than
    adding an public SSH key for access from the first master node needs
    to be done.
    """
    def __init__(self, ssh_key, os_type='ubuntu',
                 os_version="16.04"):
        """
        ssh_key is a RSA keypair (return value from create_key from util.ssl
            package)
        """
        super().__init__()
        self.ssh_key = ssh_key
        self.os_type = os_type
        self.os_version = os_version
        self.role = 'master'

        # assemble the parts for an n-th master node
        self.add_ssh_public_key(self.ssh_key)


class FirstMasterInit(NthMasterInit):
    """
    First node is a special nth node. Therefore we inherit from NthMasterInit.
    First node needs to execute bootstrap script.
    """
    def __init__(self, ssh_key, ca_bundle, cloud_config, os_type='ubuntu',
                 os_version="16.04"):
        """
        ssh_key is a RSA keypair (return value from create_key from util.ssl
            package)
        ca_bundle: The CA bundle for the CA that is used to permit accesses
            to the API server.
        cloud_config: An OSCloudConfig instance describing the information
            necessary for sending requests to the underlying cloud. Needed e.g.
            for auto scaling.
        """
        super().__init__(ssh_key, os_type, os_version)
        self.ca_bundle = ca_bundle
        self.cloud_config = cloud_config

        # assemble the parts for the first master
        # use an encoder that just returns x, since b64_cert encodes already
        # in base64 mode
        self.write_file("/etc/kubernetes/pki/ca.crt", b64_cert(ca_bundle.cert),
                        "root", "root", "0600", lambda x: x)
        self.write_file("/etc/kubernetes/pki/ca.key", b64_key(ca_bundle.key),
                        "root", "root", "0600", lambda x: x)
        self._write_cloud_config()
        self.execute_shell_script(self._get_bootstrap_script())

    def _get_bootstrap_script(self):
        name = "bootstrap-k8s-%s-%s-%s.sh" % (
            self.role, self.os_type, self.os_version)

        fh = open(resource_filename(Requirement('koris'),
                                    os.path.join(BOOTSTRAP_SCRIPTS_DIR,
                                                 name)))
        script = fh.read()
        return script

    def _write_cloud_config(self):
        """
        write out the cloud provider configuration file for OpenStack
        """
        # TODO: Password for OpenStack is included... think about security?
        content = str(self.cloud_config)
        self.write_file("/etc/kubernetes/cloud.conf", content, "root", "root",
                        "0600")


class NodeInit(BaseInit):
    """
    The node does nothing else than executing its bootstrap script.
    """
    def __init__(self, os_type='ubuntu', os_version="16.04"):
        """
        """
        super().__init__()
        self.os_type = os_type
        self.os_version = os_version
        self.role = "node"

        # assemble parts for the node
        # TODO: How to include bootstrap token? What's the exact mechanic here
        self.execute_shell_script(self._get_bootstrap_script())

        def _get_bootstrap_script(self):
            name = "bootstrap-k8s-%s-%s-%s.sh" % (
                self.role, self.os_type, self.os_version)

            fh = open(resource_filename(Requirement('koris'),
                                        os.path.join(BOOTSTRAP_SCRIPTS_DIR,
                                                     name)))
            script = fh.read()
            return script
