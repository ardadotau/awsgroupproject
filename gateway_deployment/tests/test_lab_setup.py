"""Local-only checks of the preparation boundary; no real AWS calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

SPEC = importlib.util.spec_from_file_location(
    'gateway_lab_setup', Path(__file__).resolve().parents[1] / 'tools/prepare_lab.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
ACCOUNT = '123456789012'
ROLE = 'arn:aws:iam::' + ACCOUNT + ':role/existing/lab-role'
CERT = 'arn:aws:acm:us-east-1:' + ACCOUNT + ':certificate/11111111-2222-3333-4444-555555555555'


@pytest.fixture
def environment(monkeypatch, tmp_path):
    def aws(*args):
        if args[:2] == ('sts', 'get-caller-identity'):
            return {'Account': ACCOUNT}
        if args[:2] == ('iam', 'get-instance-profile'):
            return {'InstanceProfile': {'Roles': [{'Arn': ROLE}]}}
        if args[:2] == ('acm', 'describe-certificate'):
            return {'Certificate': {'Status': 'ISSUED', 'NotAfter': '2099-01-01T00:00:00+00:00'}}
        raise AssertionError('Unexpected AWS operation: ' + repr(args))
    aws_mock = Mock(side_effect=aws)
    import_mock = Mock(return_value=SimpleNamespace(returncode=0, stdout=CERT+'\n', stderr=''))
    monkeypatch.setattr(setup, 'aws_json', aws_mock)
    monkeypatch.setattr(setup.subprocess, 'run', import_mock)
    args = SimpleNamespace(output=tmp_path/'ready.json', instance_profile='LabInstanceProfile', certificate_arn=None)
    return args, aws_mock, import_mock


def test_actual_profile_role_and_certificate_are_filled_without_new_iam(environment):
    args, aws, imported = environment
    output = setup.prepare(args)
    data = json.loads(output.read_text())
    assert data['Parameters']['ExistingRoleArn']['Default'] == ROLE
    assert data['Parameters']['CertificateArn']['Default'] == CERT
    assert data['Parameters']['ApplicationHostname']['Default'] == ''
    assert not any(x['Type'].startswith('AWS::IAM::') for x in data['Resources'].values())
    assert imported.call_count == 1
    assert {x.args[:2] for x in aws.call_args_list} == {
        ('sts','get-caller-identity'), ('iam','get-instance-profile'), ('acm','describe-certificate')}


def test_reuses_existing_certificate_without_import(environment):
    args, _, imported = environment
    args.certificate_arn = CERT
    setup.prepare(args)
    imported.assert_not_called()


def test_existing_output_stops_before_any_aws_operation(environment):
    args, aws, imported = environment
    args.output.write_text('keep existing file')
    with pytest.raises(ValueError, match='already exists'):
        setup.prepare(args)
    assert args.output.read_text() == 'keep existing file'
    aws.assert_not_called()
    imported.assert_not_called()


def test_profile_read_denial_stops_before_certificate_import(environment):
    args, aws, imported = environment
    aws.side_effect = [{'Account': ACCOUNT}, RuntimeError('AccessDenied: iam:GetInstanceProfile')]
    with pytest.raises(RuntimeError, match='AccessDenied'):
        setup.prepare(args)
    imported.assert_not_called()
    assert not args.output.exists()


def test_empty_profile_stops_before_certificate_import(environment):
    args, aws, imported = environment
    aws.side_effect = [{'Account': ACCOUNT}, {'InstanceProfile': {'Roles': []}}]
    with pytest.raises(ValueError, match='exactly one role'):
        setup.prepare(args)
    imported.assert_not_called()


def test_wrong_account_certificate_rejected_without_import(environment):
    args, _, imported = environment
    args.certificate_arn = CERT.replace(ACCOUNT, '999999999999')
    with pytest.raises(ValueError, match='this lab account'):
        setup.prepare(args)
    imported.assert_not_called()


def test_import_denied_leaves_no_prepared_template(environment):
    args, _, imported = environment
    imported.return_value = SimpleNamespace(returncode=1, stdout='', stderr='AccessDenied: acm:ImportCertificate')
    with pytest.raises(RuntimeError, match='No stack was created'):
        setup.prepare(args)
    assert not args.output.exists()


def test_invalid_certificate_state_not_used(environment):
    args, aws, _ = environment
    aws.side_effect = [{'Account': ACCOUNT}, {'InstanceProfile': {'Roles': [{'Arn': ROLE}]}},
                       {'Certificate': {'Status': 'PENDING_VALIDATION'}}]
    with pytest.raises(ValueError, match='ISSUED'):
        setup.prepare(args)
    assert not args.output.exists()
