"""Create private TLS keys locally; only root.pem is shared with clients."""
import argparse
from pathlib import Path
from tls_identity import certificate_details
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def initialize(directory):
    directory = Path(directory)
    if directory.exists():
        raise ValueError('TLS directory already exists; preserving it. Choose a new directory for a new identity.')
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.tls-setup-', dir=directory.parent))
    def run(*args): subprocess.run(['openssl', *args], check=True, capture_output=True)
    try:
        run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3650',
            # Issuer labels match the native CA entry whose modulus the
            # client patch replaces. The keys are newly generated/private.
            '-subj', '/C=US/O=GeoTrust, Inc./CN=GeoTrust SSL CA',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
            '-keyout', str(stage/'root.key'), '-out', str(stage/'root.pem'))
        run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256',
            '-subj', '/CN=winter15.gosredirector.ea.com',
            '-keyout', str(stage/'local.key'), '-out', str(stage/'local.csr'))
        (stage/'leaf.ext').write_text('basicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
            'subjectAltName=DNS:winter15.gosredirector.ea.com,DNS:gosca.ea.com,DNS:localhost,IP:127.0.0.1\n')
        run('x509', '-req', '-in', str(stage/'local.csr'), '-CA', str(stage/'root.pem'),
            '-CAkey', str(stage/'root.key'), '-set_serial', '1', '-days', '3650', '-sha256',
            '-extfile', str(stage/'leaf.ext'), '-out', str(stage/'local.pem'))
        (stage/'local.csr').unlink(); (stage/'leaf.ext').unlink()
        for p in stage.glob('*.key'): p.chmod(0o600)
        stage.rename(directory)
    finally:
        if stage.exists(): shutil.rmtree(stage)
    return directory/'root.pem'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=HERE/'tls')
    args = parser.parse_args()
    ca = initialize(args.directory)
    print('Created private server keys. Share only this public certificate: '+str(ca))
    print('Certificate SHA256 (DER): '+certificate_details(ca.read_bytes())['sha256'])


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error: raise SystemExit(str(error))
