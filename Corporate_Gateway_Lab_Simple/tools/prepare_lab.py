#!/usr/bin/env python3
"""Prepare lab-specific CloudFormation defaults without creating a stack.

Uses the current temporary AWS CLI session. Reads an EXISTING instance profile
and its actual role. Unless --certificate-arn is supplied, imports a seven-day,
browser-untrusted self-signed testing certificate using the bundled helper.
No role, instance profile, domain, database or compute resource is created.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REGION = 'us-east-1'
ROOT = Path(__file__).resolve().parents[1]


def aws_json(*args):
    result = subprocess.run(
        ['aws', *args, '--region', REGION, '--output', 'json', '--no-cli-pager'],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1200] or 'AWS CLI command failed.')
    return json.loads(result.stdout)


def prepare(args):
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError('Output already exists: ' + str(output) +
                         '. Reuse it if the certificate is still valid, or choose --output with a new filename.')
    if not output.parent.is_dir():
        raise ValueError('Output directory does not exist: ' + str(output.parent))
    template = json.loads((ROOT / 'gateway-stack.json').read_text())
    identity = aws_json('sts', 'get-caller-identity')
    account = identity['Account']
    print('Current AWS account:', account)
    print('Checking existing instance profile:', args.instance_profile)
    profile = aws_json('iam', 'get-instance-profile',
                       '--instance-profile-name', args.instance_profile)['InstanceProfile']
    roles = profile.get('Roles', [])
    if len(roles) != 1:
        raise ValueError('The existing instance profile must contain exactly one role. No IAM changes were made.')
    role_arn = roles[0]['Arn']
    if not re.fullmatch(r'arn:aws:iam::' + re.escape(account) + r':role/.+', role_arn):
        raise ValueError('The profile role is not an IAM role ARN in the active lab account.')
    print('Existing role discovered:', role_arn)
    certificate_arn = args.certificate_arn
    imported = not bool(certificate_arn)
    if certificate_arn and not re.fullmatch(
            r'arn:aws:acm:us-east-1:' + re.escape(account) + r':certificate/[A-Za-z0-9-]+',
            certificate_arn):
        raise ValueError('Use an ACM certificate ARN from this lab account in us-east-1.')
    if imported:
        print('Importing a self-signed certificate for dummy-data lab testing only.')
        print('Browser trust/name warnings are expected. No domain is purchased or configured.')
        result = subprocess.run(['bash', str(ROOT / 'tools/make_lab_certificate.sh')],
                                text=True, capture_output=True, check=False)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        if result.returncode:
            raise RuntimeError('Certificate preparation failed. ' +
                               'If ACM import is denied, keep the exact error for the tutor. No stack was created.')
        certificate_arn = result.stdout.strip()
        if not re.fullmatch(r'arn:aws:acm:us-east-1:' + re.escape(account) +
                            r':certificate/[A-Za-z0-9-]+', certificate_arn):
            raise RuntimeError('The certificate helper did not return one valid certificate ARN.')
    # Print the imported identifier before further checks so a subsequent read
    # restriction cannot hide a resource that the user may need to clean up.
    print('Certificate ARN:', certificate_arn)
    try:
        certificate = aws_json('acm', 'describe-certificate',
                               '--certificate-arn', certificate_arn)['Certificate']
        if certificate['Status'] != 'ISSUED':
            raise ValueError('Certificate status must be ISSUED, not ' + certificate['Status'])
        expiry = dt.datetime.fromisoformat(certificate['NotAfter'].replace('Z', '+00:00'))
        if expiry <= dt.datetime.now(dt.timezone.utc):
            raise ValueError('The certificate has expired.')
    except Exception:
        if imported:
            print('A test certificate was imported. Retain the ARN above for reuse or later deletion.', file=sys.stderr)
        raise
    params = template['Parameters']
    params['ExistingInstanceProfile']['Default'] = args.instance_profile
    params['ExistingRoleArn']['Default'] = role_arn
    params['CertificateArn']['Default'] = certificate_arn
    params['ApplicationHostname']['Default'] = ''
    template.setdefault('Metadata', {})['LearnerLabPreparation'] = {
        'PreparedAtUTC': dt.datetime.now(dt.timezone.utc).isoformat(),
        'CertificateMode': 'SelfSignedLabOnly' if imported else 'ExistingCertificateVerifyHostname',
        'CertificateExpires': certificate['NotAfter'],
        'Note': 'Current existing profile and role verified. Creation permissions and credits not verified. No stack deployed.',
    }
    # Exclusive create avoids silently replacing an earlier prepared template.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(template, stream, indent=2)
        stream.write('\n')
    print('\nREADY TO UPLOAD:', output)
    print('In CloudShell, Actions > Download file: paste the full path above.')
    print('Upload ' + output.name + ' in CloudFormation; enter AlertEmail.')
    print('Keep the discovered role/profile/certificate defaults. Leave ApplicationHostname blank for no domain.')
    print('The AWS load-balancer HTTPS address will be in stack Outputs after deployment.')
    print('Certificate expiry:', certificate['NotAfter'])
    print('A self-signed or hostname-mismatched certificate causes browser warnings; use dummy files and disclose it.')
    print('This command has not deployed the stack or verified service-creation permissions or sufficient lab credits.')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-profile', default='LabInstanceProfile')
    parser.add_argument('--certificate-arn', help='Reuse an existing issued ACM certificate; skips test-certificate import.')
    parser.add_argument('--output', type=Path, default=Path('gateway-stack-ready.json'))
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9+=,.@_-]+', args.instance_profile):
        parser.error('Supply an instance profile NAME, not an ARN.')
    try:
        prepare(args)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print('SETUP STOPPED:', exc, file=sys.stderr)
        print('No IAM role was created or modified and no CloudFormation stack was launched.', file=sys.stderr)
        print('AccessDenied does not prove a role/profile is missing. Check the lab instructions or ask the tutor with the exact error.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
