import base64
import datetime
import ipaddress

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography import utils


def create_key(size=2048, public_exponent=65537):
    key = rsa.generate_private_key(
        public_exponent=public_exponent,
        key_size=size,
        backend=default_backend()
    )
    return key


def create_certificate(private_key, public_key, country,
                       state_province, locality, orga, name, hosts, ips):

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, state_province),
        x509.NameAttribute(NameOID.LOCALITY_NAME, locality),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, orga),
        x509.NameAttribute(NameOID.COMMON_NAME, name),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        public_key
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_after(
        # Our certificate will be valid for 1800 days
        datetime.datetime.utcnow() + datetime.timedelta(days=1800))

    alt_names = [x509.DNSName(host) for host in hosts]

    if ips:
        alt_names.extend(x509.IPAddress(ipaddress.IPv4Address(ip))
                         for ip in ips)

    cert.add_extension(
        x509.SubjectAlternativeName(alt_names),
        critical=False)

    cert = cert.sign(private_key, hashes.SHA256(), default_backend())

    return cert


def b64_key(key):
    """encode private bytes of a key to base64"""

    bytes_args = dict(encoding=serialization.Encoding.PEM,
                      format=serialization.PrivateFormat.TraditionalOpenSSL,
                      encryption_algorithm=serialization.NoEncryption())

    key_bytes = key.private_bytes(**bytes_args)

    return base64.b64encode(key_bytes).decode()


def b64_cert(cert):
    """encode public bytes of a cert to base64"""
    return base64.b64encode(
        cert.public_bytes(serialization.Encoding.PEM)).decode()
