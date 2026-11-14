#!/usr/bin/env python3
"""Pack app, upload to S3, import lab cert, write ready CloudFormation JSON.

Different approach from the embedded-UserData package:
  1) Upload bootstrap.sh + app.tgz to a private S3 bucket
  2) Fill CertificateArn / LabRole / BootstrapBucket into gateway-stack.json
  3) You upload the ready JSON in CloudFormation (AlertEmail only)

Creates NO IAM roles and does NOT create the stack.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time

REGION = 'us-east-1'
ROOT = Path(__file__).resolve().parents[1]


def run(cmd, check=True):
    print('+', ' '.join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'command failed')[:1500])
    return result


def aws_json(*args):
    result = run(['aws', *args, '--region', REGION, '--output', 'json', '--no-cli-pager'])
    return json.loads(result.stdout)


def make_app_tarball(dest: Path):
    src = ROOT / 'src'
    required = ['app.py', 'install.sh', 'requirements.txt', 'templates/index.html', 'templates/login.html']
    for name in required:
        if not (src / name).is_file():
            raise FileNotFoundError('Missing ' + name)
    with tarfile.open(dest, 'w:gz') as archive:
        for path in sorted(src.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            archive.add(path, arcname=str(path.relative_to(src)))
    print('Packed app tarball:', dest, dest.stat().st_size, 'bytes')


def ensure_bucket(name: str, role_arn: str):
    try:
        run(['aws', 's3api', 'head-bucket', '--bucket', name, '--region', REGION], check=True)
        print('Using existing bootstrap bucket:', name)
    except RuntimeError:
        print('Creating bootstrap bucket:', name)
        # us-east-1 LocationConstraint must be omitted
        run(['aws', 's3api', 'create-bucket', '--bucket', name, '--region', REGION])
        run(['aws', 's3api', 'put-public-access-block', '--bucket', name,
             '--public-access-block-configuration',
             'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'])
    policy = {
        'Version': '2012-10-17',
        'Statement': [{
            'Sid': 'LabRoleReadBootstrap',
            'Effect': 'Allow',
            'Principal': {'AWS': role_arn},
            'Action': ['s3:GetObject'],
            'Resource': f'arn:aws:s3:::{name}/*',
        }],
    }
    run(['aws', 's3api', 'put-bucket-policy', '--bucket', name, '--policy', json.dumps(policy)])


def upload(bucket: str, prefix: str, local: Path, key_name: str):
    key = prefix + key_name
    run(['aws', 's3', 'cp', str(local), f's3://{bucket}/{key}', '--region', REGION])
    print('Uploaded s3://%s/%s' % (bucket, key))


def prepare(args):
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError('Output exists: %s — pick a new --output name' % output)
    template_path = ROOT / 'gateway-stack.json'
    if not template_path.is_file():
        raise FileNotFoundError('Run: python3 build_template.py')

    identity = aws_json('sts', 'get-caller-identity')
    account = identity['Account']
    print('Account:', account)

    profile = aws_json('iam', 'get-instance-profile',
                       '--instance-profile-name', args.instance_profile)['InstanceProfile']
    roles = profile.get('Roles', [])
    if len(roles) != 1:
        raise ValueError('LabInstanceProfile must have exactly one role')
    role_arn = roles[0]['Arn']
    print('Role:', role_arn)

    # Certificate
    if args.certificate_arn:
        certificate_arn = args.certificate_arn
    else:
        print('Importing self-signed lab certificate...')
        result = run(['bash', str(ROOT / 'tools/make_lab_certificate.sh')], check=True)
        certificate_arn = result.stdout.strip().splitlines()[-1].strip()
    if not re.fullmatch(r'arn:aws:acm:us-east-1:' + re.escape(account) + r':certificate/[A-Za-z0-9-]+',
                        certificate_arn):
        raise RuntimeError('Bad certificate ARN: ' + certificate_arn)
    print('Certificate:', certificate_arn)

    bucket = args.bucket or ('gw-boot-%s-%s' % (account[-6:], int(time.time()) % 1000000))
    prefix = args.prefix if args.prefix.endswith('/') else args.prefix + '/'
    ensure_bucket(bucket, role_arn)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / 'app.tgz'
        make_app_tarball(tarball)
        boot = ROOT / 'tools/bootstrap_instance.sh'
        if not boot.is_file():
            raise FileNotFoundError(boot)
        upload(bucket, prefix, boot, 'bootstrap.sh')
        upload(bucket, prefix, tarball, 'app.tgz')

    template = json.loads(template_path.read_text())
    params = template['Parameters']
    params['ExistingInstanceProfile']['Default'] = args.instance_profile
    params['ExistingRoleArn']['Default'] = role_arn
    params['CertificateArn']['Default'] = certificate_arn
    params['BootstrapBucket']['Default'] = bucket
    params['BootstrapPrefix']['Default'] = prefix
    template.setdefault('Metadata', {})['S3BootstrapPreparation'] = {
        'PreparedAtUTC': dt.datetime.now(dt.timezone.utc).isoformat(),
        'BootstrapBucket': bucket,
        'BootstrapPrefix': prefix,
        'CertificateArn': certificate_arn,
        'Note': 'App code is on S3. UserData only downloads and runs it. No Fn::Sub in template.',
    }

    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(template, stream, indent=2)
        stream.write('\n')

    print('\nREADY:', output)
    print('Download this file from CloudShell, then Create stack.')
    print('Enter AlertEmail only. Leave CFN service role BLANK (do not choose LabRole there).')
    print('Bootstrap bucket (keep until stack is healthy):', bucket)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-profile', default='LabInstanceProfile')
    parser.add_argument('--certificate-arn')
    parser.add_argument('--bucket', help='Optional existing/new bootstrap bucket name')
    parser.add_argument('--prefix', default='gateway/')
    parser.add_argument('--output', type=Path, default=Path('gateway-stack-ready-s3.json'))
    args = parser.parse_args(argv)
    try:
        prepare(args)
        return 0
    except Exception as exc:
        print('SETUP STOPPED:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
