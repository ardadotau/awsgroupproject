"""INFS3701 Corporate Cloud Storage Gateway.

Group extension of Arda Idrisoglu's upload/checksum service.
Production path: PostgreSQL session/metadata store + private S3 objects.
GATEWAY_PREVIEW=1 enables synthetic local data ONLY for development screenshots.
"""
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import quote

import boto3
from botocore.config import Config
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('gateway')
DEPARTMENTS = {'Cyber Security', 'Finance', 'Human Resources', 'Executive General'}
MAX_FILE = 16 * 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc)


def sha256_stream(stream):
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(4096), b''):
        size += len(chunk)
        if size > MAX_FILE:
            raise ValueError('Choose a file no larger than 16 MB.')
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest(), size


class Services:
    def __init__(self, config):
        self.cfg = config
        self._clients, self._secrets, self._lock = {}, {}, threading.Lock()

    def client(self, name):
        with self._lock:
            if name not in self._clients:
                self._clients[name] = boto3.client(name, region_name=self.cfg['region'], config=Config(
                    connect_timeout=3, read_timeout=5, retries={'max_attempts': 1}))
            return self._clients[name]

    def secret(self, arn, refresh=False):
        with self._lock:
            cached = self._secrets.get(arn)
        if cached and cached[0] > time.monotonic() and not refresh:
            return cached[1]
        value = json.loads(self.client('secretsmanager').get_secret_value(SecretId=arn)['SecretString'])
        with self._lock:
            self._secrets[arn] = (time.monotonic() + 60, value)
        return value

    def connect(self, master=False):
        arn = self.cfg['db_master_secret' if master else 'db_app_secret']
        for attempt in range(2):
            creds = self.secret(arn, refresh=bool(attempt))
            try:
                return psycopg2.connect(host=self.cfg['db_host'], port=5432, dbname=self.cfg['db_name'],
                    user=creds['username'], password=creds['password'], connect_timeout=5,
                    sslmode='verify-full', sslrootcert=self.cfg.get('certificate_ca', '/opt/gateway/rds-ca.pem'),
                    options='-c search_path=gateway,public -c statement_timeout=15000')
            except psycopg2.OperationalError:
                if attempt:
                    raise

    def initialize(self):
        """Serialize two-server setup; app role owns only its own schema."""
        app_creds = self.secret(self.cfg['db_app_secret'])
        connection = self.connect(master=True)
        try:
            with connection:
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_advisory_xact_lock(194628031)')
                    cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (app_creds['username'],))
                    if cur.fetchone() is None:
                        cur.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(
                            sql.Identifier(app_creds['username']), sql.Literal(app_creds['password'])))
                    else:
                        cur.execute(sql.SQL('ALTER ROLE {} WITH LOGIN PASSWORD {}').format(
                            sql.Identifier(app_creds['username']), sql.Literal(app_creds['password'])))
                    cur.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                        sql.Identifier(self.cfg['db_name']), sql.Identifier(app_creds['username'])))
                    # RDS master is not a PostgreSQL superuser. Explicit SET-role
                    # membership permits creating the schema for the app owner.
                    cur.execute(sql.SQL('GRANT {} TO CURRENT_USER').format(sql.Identifier(app_creds['username'])))
                    cur.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS gateway AUTHORIZATION {}').format(
                        sql.Identifier(app_creds['username'])))
        finally:
            connection.close()
        with self.transaction() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(194628032)')
            cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
                token_hash CHAR(64) PRIMARY KEY, username TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts (
                key TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads (
                id UUID PRIMARY KEY, filename TEXT NOT NULL, department TEXT NOT NULL,
                sha256 CHAR(64) NOT NULL, size BIGINT NOT NULL CHECK(size >= 0),
                object_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','complete','failed')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                server TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS uploads_newest ON uploads(created_at DESC);
                CREATE TABLE IF NOT EXISTS audit_events (
                id BIGSERIAL PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                event TEXT NOT NULL, server TEXT NOT NULL, detail TEXT NOT NULL);''')

    @contextlib.contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur
        finally:
            conn.close()

    def health(self):
        with self.transaction() as cur:
            cur.execute('SELECT 1 FROM gateway.uploads LIMIT 1')
        return True

    def event(self, event, detail):
        with self.transaction() as cur:
            cur.execute('INSERT INTO audit_events(event,server,detail) VALUES(%s,%s,%s)',
                (event, self.cfg['instance_name'], detail[:300]))
        LOG.info('event=%s server=%s', event, self.cfg['instance_name'])

    def blocked(self, key):
        with self.transaction() as cur:
            cur.execute("SELECT failures FROM login_attempts WHERE key=%s AND window_start > now()-interval '10 minutes'", (key,))
            row = cur.fetchone()
            return bool(row and row['failures'] >= 5)

    def attempt(self, key, success):
        with self.transaction() as cur:
            if success:
                cur.execute('DELETE FROM login_attempts WHERE key=%s', (key,))
            else:
                cur.execute("""INSERT INTO login_attempts VALUES(%s,1,now()) ON CONFLICT(key) DO UPDATE SET
                    failures=CASE WHEN login_attempts.window_start < now()-interval '10 minutes' THEN 1 ELSE login_attempts.failures+1 END,
                    window_start=CASE WHEN login_attempts.window_start < now()-interval '10 minutes' THEN now() ELSE login_attempts.window_start END""", (key,))

    def new_session(self, token_hash, username):
        with self.transaction() as cur:
            cur.execute('DELETE FROM sessions WHERE expires_at < now()')
            cur.execute("INSERT INTO sessions VALUES(%s,%s,now()+interval '8 hours')", (token_hash, username))

    def get_session(self, token_hash):
        with self.transaction() as cur:
            cur.execute('SELECT username FROM sessions WHERE token_hash=%s AND expires_at>now()', (token_hash,))
            row = cur.fetchone()
            return row['username'] if row else None

    def delete_session(self, token_hash):
        with self.transaction() as cur:
            cur.execute('DELETE FROM sessions WHERE token_hash=%s', (token_hash,))

    def records(self):
        with self.transaction() as cur:
            cur.execute("SELECT id,filename,department,sha256,size,created_at,server,status FROM uploads WHERE status='complete' ORDER BY created_at DESC LIMIT 250")
            records = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT count(*) AS count,coalesce(sum(size),0) AS total_bytes,count(DISTINCT department) AS departments FROM uploads WHERE status='complete'")
            summary = dict(cur.fetchone())
        for record in records:
            record['id'] = str(record['id'])
            record['created_at'] = record['created_at'].isoformat()
        return records, summary

    def reserve(self, item):
        with self.transaction() as cur:
            cur.execute("""INSERT INTO uploads(id,filename,department,sha256,size,object_key,status,server)
                VALUES(%(id)s,%(filename)s,%(department)s,%(sha256)s,%(size)s,%(object_key)s,'pending',%(server)s)
                ON CONFLICT(id) DO NOTHING RETURNING id""", item)
            if cur.fetchone():
                return 'new'
            cur.execute('SELECT * FROM uploads WHERE id=%s FOR UPDATE', (item['id'],))
            old = cur.fetchone()
            if any(old[k] != item[k] for k in ['sha256','size','filename','department']):
                return 'conflict'
            if old['status'] == 'complete':
                return 'complete'
            if old['status'] == 'pending' and old['updated_at'] > utcnow()-timedelta(minutes=5):
                return 'busy'
            cur.execute("UPDATE uploads SET status='pending',updated_at=now(),server=%s WHERE id=%s", (item['server'], item['id']))
            return 'new'

    def finish(self, item_id, success):
        with self.transaction() as cur:
            cur.execute('UPDATE uploads SET status=%s,updated_at=now() WHERE id=%s', ('complete' if success else 'failed', item_id))

    def download(self, item_id):
        with self.transaction() as cur:
            cur.execute("SELECT filename,object_key FROM uploads WHERE id=%s AND status='complete'", (item_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def put_file(self, stream, item):
        self.client('s3').upload_fileobj(stream, self.cfg['bucket'], item['object_key'],
            ExtraArgs={'ServerSideEncryption':'AES256','ContentType':'application/octet-stream',
                       'Metadata':{'sha256':item['sha256'],'upload-id':item['id']}})

    def get_file(self, key):
        return self.client('s3').get_object(Bucket=self.cfg['bucket'], Key=key)['Body']

    def target_health(self):
        arn = self.cfg.get('target_group_arn')
        if not arn:
            groups = self.client('elbv2').describe_target_groups(Names=[self.cfg['target_group_name']])
            arn = groups['TargetGroups'][0]['TargetGroupArn']
        response = self.client('elbv2').describe_target_health(TargetGroupArn=arn)
        return [{'id':x['Target']['Id'],'state':x['TargetHealth']['State'],
                 'reason':x['TargetHealth'].get('Description','')} for x in response['TargetHealthDescriptions']]

    def begin_test(self):
        # Shared lock prevents two operators taking both instances out at once.
        with self.transaction() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(194628033)')
            cur.execute("SELECT 1 FROM audit_events WHERE event='failure_test_started' AND occurred_at>now()-interval '4 minutes' LIMIT 1")
            if cur.fetchone():
                return False
            cur.execute('INSERT INTO audit_events(event,server,detail) VALUES(%s,%s,%s)',
                ('failure_test_started',self.cfg['instance_name'],'Automatic recovery in 240 seconds'))
        return True

    def status(self):
        result = {'alarms':[], 'targets':[], 'notifications':{'confirmed':None,'message':'Confirmation not checked.'}}
        errors = []
        try:
            self.health()
            result['database'] = {'ok':True,'message':'PostgreSQL connection verified over TLS.'}
        except Exception:
            result['database'] = {'ok':False,'message':'Database check failed. Inspect the server logs.'}
        try:
            self.client('s3').list_objects_v2(Bucket=self.cfg['bucket'],Prefix='assets/',MaxKeys=1)
            result['storage'] = {'ok':True,'message':'Private S3 bucket is reachable.'}
        except Exception:
            result['storage'] = {'ok':False,'message':'Storage access could not be verified.'}
        try:
            result['targets'] = self.target_health()
        except Exception:
            errors.append('Target health is unavailable with the current lab permissions.')
        try:
            data = self.client('cloudwatch').describe_alarms(AlarmNamePrefix=self.cfg['stack_name']+'-')
            result['alarms'] = [{'name':a['AlarmName'],'state':a['StateValue'],'reason':a.get('StateReason','')} for a in data['MetricAlarms']]
        except Exception:
            errors.append('Alarm state is unavailable with the current lab permissions.')
        try:
            subs = self.client('sns').list_subscriptions_by_topic(TopicArn=self.cfg['topic_arn'])['Subscriptions']
            email_subs = [x for x in subs if x['Protocol']=='email']
            confirmed = any(x['SubscriptionArn'] not in ('PendingConfirmation','Deleted') for x in email_subs)
            result['notifications'] = {'confirmed':confirmed,'message':
                'Email subscription confirmed. Actual delivery must be verified in your inbox.' if confirmed
                else 'Open the AWS SNS confirmation email and click Confirm subscription.'}
        except Exception:
            errors.append('Email confirmation state is unavailable; verify it in the SNS console.')
        if errors:
            result['aws_error'] = ' '.join(errors)
        return result


class PreviewServices:
    """Only for explicit offline preview and tests; never used by the template."""
    def __init__(self, cfg):
        self.cfg, self.sessions, self.rows, self.attempts = cfg, {}, {}, {}

    def secret(self, arn, **kwargs):
        return {'username':'admin','password':'preview-only-change-me'}

    def health(self): return True
    def event(self, *args): pass
    def blocked(self, key): return self.attempts.get(key,0) >= 5
    def attempt(self, key, success): self.attempts[key] = 0 if success else self.attempts.get(key,0)+1
    def new_session(self, token_hash, username): self.sessions[token_hash] = username
    def get_session(self, token_hash): return self.sessions.get(token_hash)
    def delete_session(self, token_hash): self.sessions.pop(token_hash,None)
    def records(self):
        records = [r.copy() for r in self.rows.values() if r['status']=='complete']
        return records, {'count':len(records),'total_bytes':sum(r['size'] for r in records),'departments':len(set(r['department'] for r in records))}
    def reserve(self, item):
        old = self.rows.get(item['id'])
        if old:
            if any(old[k]!=item[k] for k in ['sha256','size','filename','department']): return 'conflict'
            return 'complete' if old['status']=='complete' else 'busy'
        self.rows[item['id']] = dict(item, status='pending', created_at=utcnow().isoformat())
        return 'new'
    def finish(self, item_id, success): self.rows[item_id]['status'] = 'complete' if success else 'failed'
    def put_file(self, stream, item): self.rows[item['id']]['content'] = stream.read()
    def download(self, item_id): return self.rows.get(item_id)
    def get_file(self, key):
        return io.BytesIO(next(r['content'] for r in self.rows.values() if r['object_key']==key))
    def target_health(self): return [{'id':'preview-app-a','state':'healthy'},{'id':'preview-app-b','state':'healthy'}]
    def begin_test(self):
        if getattr(self,'test_started',False): return False
        self.test_started=True
        return True
    def status(self):
        return {'database':{'ok':True,'message':'Synthetic preview data; no AWS connection.'},
            'storage':{'ok':True,'message':'Synthetic preview data; no AWS connection.'},
            'alarms':[{'name':'preview-healthy-targets','state':'OK','reason':'Illustrative local preview only.'}],
            'targets':self.target_health(),'notifications':{'confirmed':None,'message':'Offline preview. No email subscription or delivery.'}}


def create_app(config=None, services=None):
    preview = os.getenv('GATEWAY_PREVIEW') == '1'
    if config is None:
        if preview:
            config = {'region':'us-east-1','instance_name':'preview-app-a','app_secret':'preview',
                      'stack_name':'local-preview','dashboard_name':'local-preview'}
        else:
            config = json.loads(Path(os.getenv('GATEWAY_CONFIG','/etc/gateway/runtime.json')).read_text())
    preview = bool(config.get('preview',preview))
    service = services or (PreviewServices(config) if preview else Services(config))
    credentials = service.secret(config['app_secret'])
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.secret_key = hmac.new(credentials['password'].encode(), b'gateway-csrf-signing-v1', hashlib.sha256).digest()
    app.config.update(MAX_CONTENT_LENGTH=MAX_FILE+1024*1024, SESSION_COOKIE_SECURE=not preview,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    app.extensions['gateway_services'] = service
    dashboard_url = 'https://'+config['region']+'.console.aws.amazon.com/cloudwatch/home?region='+config['region']+'#dashboards:name='+quote(config.get('dashboard_name',''))
    failure_file = Path(config.get('failure_file','/run/gateway/failure-until'))
    status_cache, cache_lock = {}, threading.Lock()

    def failure_until():
        try: return float(failure_file.read_text())
        except (OSError,ValueError): return 0

    def csrf_token():
        if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(24)
        return session['csrf']

    def authenticated(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            token = request.cookies.get('gateway_auth','')
            g.token_hash = hashlib.sha256(token.encode()).hexdigest()
            g.user = service.get_session(g.token_hash) if token else None
            if not g.user:
                if request.path.startswith('/api/'):
                    return jsonify(error='Your session has expired. Sign in again.'),401
                return redirect(url_for('login'))
            return fn(*args,**kwargs)
        return inner

    @app.before_request
    def protect_posts():
        if request.method in ('POST','PUT','DELETE','PATCH'):
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token','')
            if not session.get('csrf') or not hmac.compare_digest(str(supplied).encode(), session['csrf'].encode()):
                return jsonify(error='Security token expired. Refresh the page and try again.'),400

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        if not preview: response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            message = 'Choose a file no larger than 16 MB.' if exc.code==413 else exc.description
            return jsonify(error=message),exc.code
        LOG.error('request_failed type=%s path=%s', type(exc).__name__, request.path)
        return jsonify(error='A required service is temporarily unavailable. Please retry shortly.'),503

    @app.route('/health')
    def health():
        if failure_until() > time.time():
            return jsonify(ok=False,server=config['instance_name'],reason='Temporary failure test'),503
        try:
            service.health()
            return jsonify(ok=True,server=config['instance_name'],zone=config.get('zone','preview'))
        except Exception:
            return jsonify(ok=False,server=config['instance_name'],reason='Database unavailable'),503

    @app.route('/login', methods=['GET','POST'])
    def login():
        error_message = None
        if request.method == 'POST':
            key = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
            if service.blocked(key):
                return render_template('login.html',csrf_token=csrf_token(),error='Too many attempts. Try again after 10 minutes.',preview=preview),429
            current = service.secret(config['app_secret'])
            valid_user = hmac.compare_digest(request.form.get('username','').encode(),current['username'].encode())
            valid_password = hmac.compare_digest(request.form.get('password','').encode(),current['password'].encode())
            success = valid_user and valid_password
            service.attempt(key,success)
            if success:
                raw = secrets.token_urlsafe(32)
                service.new_session(hashlib.sha256(raw.encode()).hexdigest(),current['username'])
                session['csrf'] = secrets.token_urlsafe(24)
                response = redirect(url_for('index'))
                response.set_cookie('gateway_auth',raw,max_age=8*3600,secure=not preview,httponly=True,samesite='Lax')
                return response
            error_message = 'The username or password is incorrect.'
        return render_template('login.html',csrf_token=csrf_token(),error=error_message,preview=preview),401 if error_message else 200

    @app.route('/logout', methods=['POST'])
    @authenticated
    def logout():
        service.delete_session(g.token_hash)
        session.clear()
        response = redirect(url_for('login'))
        response.delete_cookie('gateway_auth')
        return response

    @app.route('/')
    @authenticated
    def index():
        return render_template('index.html',csrf_token=csrf_token(),username=g.user,
            server_name=config['instance_name'],region=config['region'],dashboard_url=dashboard_url,preview=preview)

    @app.route('/api/records')
    @authenticated
    def records():
        rows, summary = service.records()
        for row in rows: row.pop('content',None)
        return jsonify(records=rows,summary=summary,server=config['instance_name'],region=config['region'])

    @app.route('/api/upload', methods=['POST'])
    @authenticated
    def upload():
        document = request.files.get('document')
        department = request.form.get('department','')
        if document is None or not document.filename:
            return jsonify(error='Choose a document to upload.'),400
        if department not in DEPARTMENTS:
            return jsonify(error='Choose a valid department.'),400
        filename = secure_filename(document.filename)[:180]
        if not filename: return jsonify(error='Choose a file with a valid filename.'),400
        try:
            item_id = str(uuid.UUID(request.form.get('upload_id','')))
            digest, size = sha256_stream(document.stream)
        except ValueError as exc:
            return jsonify(error=str(exc) if '16 MB' in str(exc) else 'The upload identifier is invalid. Refresh and retry.'),400
        if size == 0: return jsonify(error='Empty files cannot be uploaded.'),400
        item = dict(id=item_id,filename=filename,department=department,sha256=digest,size=size,
                    object_key='assets/'+item_id,server=config['instance_name'])
        state = service.reserve(item)
        if state == 'complete': return jsonify(ok=True,id=item_id,message='This upload is already saved.'),200
        if state in ('conflict','busy'):
            return jsonify(error='This upload is already in progress or its contents changed. Retry later or choose the file again.'),409
        try:
            service.put_file(document.stream,item)
            service.finish(item_id,True)
        except Exception:
            try: service.finish(item_id,False)
            except Exception: pass
            raise
        try: service.event('upload_complete',item_id)
        except Exception: LOG.warning('audit_event_failed upload=%s', item_id)
        return jsonify(ok=True,id=item_id,message='Document uploaded and its record saved.'),201

    @app.route('/download/<uuid:item_id>')
    @authenticated
    def download(item_id):
        item = service.download(str(item_id))
        if not item: return jsonify(error='Document not found.'),404
        body = service.get_file(item['object_key'])
        def chunks():
            try:
                for chunk in iter(lambda: body.read(64*1024), b''): yield chunk
            finally: body.close()
        response = Response(stream_with_context(chunks()), mimetype='application/octet-stream')
        response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''"+quote(item['filename'])
        return response

    @app.route('/api/status')
    @authenticated
    def status():
        with cache_lock:
            cached = status_cache.get('value')
            if not cached or status_cache.get('expires',0) < time.monotonic():
                cached = service.status()
                status_cache.update(value=cached,expires=time.monotonic()+15)
        until = failure_until()
        return jsonify(**cached,server=config['instance_name'],region=config['region'],dashboard_url=dashboard_url,
            preview=preview,test={'active':until>time.time(),'until':until})

    @app.route('/api/test/failure', methods=['POST'])
    @authenticated
    def failure_test():
        if preview:
            return jsonify(error='Failure tests are disabled in the offline preview.'),400
        targets = service.target_health()
        if sum(t['state']=='healthy' for t in targets) < 2:
            return jsonify(error='Start a test only when at least two targets are healthy.'),409
        if not service.begin_test():
            return jsonify(error='Another failure test is already running. Wait four minutes before trying again.'),409
        until = time.time()+240
        failure_file.parent.mkdir(parents=True,exist_ok=True)
        temporary = failure_file.with_suffix('.new')
        temporary.write_text(str(until));os.replace(temporary,failure_file)
        return jsonify(message='This server will fail health checks for four minutes, then recover automatically.',until=until,server=config['instance_name'])

    return app


if __name__ == '__main__':
    if sys.argv[1:] == ['init-db']:
        cfg = json.loads(Path(os.getenv('GATEWAY_CONFIG','/etc/gateway/runtime.json')).read_text())
        Services(cfg).initialize()
        print('Database schema and restricted application user initialized.')
    else:
        create_app().run(host='127.0.0.1',port=5000,debug=False)
else:
    app = create_app() if os.getenv('GATEWAY_CONFIG') or os.getenv('GATEWAY_PREVIEW')=='1' else None
