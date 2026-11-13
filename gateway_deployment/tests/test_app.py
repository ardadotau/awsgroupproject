"""Local checks. AWS and PostgreSQL integration are separate deployment tests."""
import io
import importlib.util
from pathlib import Path
import re
import sys
import time
import uuid

import pytest

SRC=Path(__file__).resolve().parents[1]/'src'
spec=importlib.util.spec_from_file_location('gateway_app',SRC/'app.py')
gateway=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=gateway
spec.loader.exec_module(gateway)


@pytest.fixture
def system(tmp_path):
    cfg={'region':'us-east-1','instance_name':'app-a','app_secret':'test-secret',
         'stack_name':'test-stack','dashboard_name':'test','preview':True,
         'failure_file':str(tmp_path/'failure-until')}
    service=gateway.PreviewServices(cfg)
    app=gateway.create_app(cfg,service)
    app.config['TESTING']=True
    return app,service,cfg


def csrf(client,path='/login'):
    client.get(path)
    with client.session_transaction() as session:
        return session['csrf']


def login(client):
    token=csrf(client)
    response=client.post('/login',data={'username':'admin','password':'preview-only-change-me','csrf_token':token})
    assert response.status_code==302
    with client.session_transaction() as session:return session['csrf']


def upload(client,token,body=b'hello',item_id=None,filename='document.txt',department='Finance'):
    return client.post('/api/upload',headers={'X-CSRF-Token':token},data={
        'document':(io.BytesIO(body),filename),'department':department,'upload_id':item_id or str(uuid.uuid4())})


def test_server_authentication_cannot_be_bypassed_by_browser_storage(system):
    client=system[0].test_client()
    assert client.get('/').status_code==302
    assert client.get('/api/records').status_code==401
    assert client.get('/download/'+str(uuid.uuid4())).status_code==302


def test_login_requires_csrf_and_rejects_wrong_password(system):
    client=system[0].test_client()
    assert client.post('/login',data={'username':'admin','password':'preview-only-change-me'}).status_code==400
    token=csrf(client)
    assert client.post('/login',data={'username':'admin','password':'wrong','csrf_token':token}).status_code==401
    assert client.post('/login',data={'username':'अनजान','password':'गलत','csrf_token':token}).status_code==401
    assert client.post('/login',data={'username':'admin','password':'x','csrf_token':'गलत'}).status_code==400


def test_login_rate_limit_shared_repository(system):
    client=system[0].test_client();token=csrf(client)
    for _ in range(5):
        assert client.post('/login',data={'username':'admin','password':'bad','csrf_token':token}).status_code==401
    assert client.post('/login',data={'username':'admin','password':'preview-only-change-me','csrf_token':token}).status_code==429


def test_upload_hash_download_and_idempotent_retry(system):
    client=system[0].test_client();token=login(client);item_id=str(uuid.uuid4())
    response=upload(client,token,item_id=item_id)
    assert response.status_code==201
    record=client.get('/api/records').json['records'][0]
    assert record['sha256']==gateway.hashlib.sha256(b'hello').hexdigest()
    assert record['filename']=='document.txt'
    assert client.get('/download/'+item_id).data==b'hello'
    assert upload(client,token,item_id=item_id).status_code==200
    assert client.get('/api/records').json['summary']['count']==1
    assert upload(client,token,body=b'different',item_id=item_id).status_code==409


def test_failed_storage_write_is_not_shown_as_complete(system):
    app,service,cfg=system;client=app.test_client();token=login(client)
    def fail(*args): raise RuntimeError('simulated S3 failure')
    service.put_file=fail
    assert upload(client,token).status_code==503
    assert client.get('/api/records').json['records']==[]
    assert next(iter(service.rows.values()))['status']=='failed'


def test_records_and_sessions_are_shared_between_app_instances(system):
    app,service,cfg=system;first=app.test_client();token=login(first)
    assert upload(first,token).status_code==201
    second_cfg=dict(cfg,instance_name='app-b')
    second=gateway.create_app(second_cfg,service).test_client()
    for name in ['session','gateway_auth']:
        second.set_cookie(name,first.get_cookie(name).value)
    response=second.get('/api/records')
    assert response.status_code==200
    assert response.json['server']=='app-b'
    assert response.json['summary']['count']==1
    assert second.post('/logout',data={'csrf_token':token}).status_code==302
    assert first.get('/api/records').status_code==401


def test_upload_validation_and_csrf(system):
    client=system[0].test_client();token=login(client)
    assert upload(client,'incorrect').status_code==400
    assert upload(client,token,body=b'').status_code==400
    assert upload(client,token,department='Unknown').status_code==400
    assert upload(client,token,body=b'a'*(16*1024*1024+1)).status_code==400
    assert upload(client,token,filename='../../invoice.txt').status_code==201
    assert client.get('/api/records').json['records'][0]['filename']=='invoice.txt'


def test_fault_test_requires_auth_and_two_healthy_servers_and_recovers(system):
    app,service,cfg=system
    # Non-preview branch, with fake services; cookie requests use HTTPS.
    cfg['preview']=False;app=gateway.create_app(cfg,service);client=app.test_client()
    client.get('/login',base_url='https://localhost')
    with client.session_transaction() as session:token=session['csrf']
    response=client.post('/login',base_url='https://localhost',data={'username':'admin','password':'preview-only-change-me','csrf_token':token})
    assert response.status_code==302
    with client.session_transaction() as session:token=session['csrf']
    original=service.target_health
    service.target_health=lambda:[{'id':'app-a','state':'healthy'}]
    assert client.post('/api/test/failure',base_url='https://localhost',headers={'X-CSRF-Token':token}).status_code==409
    service.target_health=original
    response=client.post('/api/test/failure',base_url='https://localhost',headers={'X-CSRF-Token':token})
    assert response.status_code==200
    assert client.get('/health').status_code==503
    # Even if target health has not caught up, shared reservation blocks a second test.
    assert client.post('/api/test/failure',base_url='https://localhost',headers={'X-CSRF-Token':token}).status_code==409
    Path(cfg['failure_file']).write_text(str(time.time()-1))
    assert client.get('/health').status_code==200


def test_secret_is_not_exposed_by_status_or_error(system):
    client=system[0].test_client();login(client)
    response=client.get('/api/status')
    assert 'preview-only-change-me' not in response.get_data(as_text=True)
    assert response.json['preview'] is True


def test_deployment_postgres_connections_verify_certificate_and_hostname():
    cfg={'region':'us-east-1','db_app_secret':'test','db_host':'database.example',
         'db_name':'gateway','certificate_ca':'/test/ca.pem'}
    service=gateway.Services(cfg)
    service.secret=lambda *a,**k:{'username':'gateway_app','password':'temporary-test-value'}
    called={};original=gateway.psycopg2.connect
    try:
        def connect(**kwargs):called.update(kwargs);return object()
        gateway.psycopg2.connect=connect
        service.connect()
    finally:gateway.psycopg2.connect=original
    assert called['sslmode']=='verify-full'
    assert called['sslrootcert']=='/test/ca.pem'
