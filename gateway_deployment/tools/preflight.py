#!/usr/bin/env python3
"""Read-only prerequisite checks using your CURRENT temporary AWS CLI session.
Does not prove that resource-creation permissions or sufficient credits exist.
"""
import argparse
import json
import subprocess
import sys

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--certificate-arn',required=True)
    p.add_argument('--role-arn',help='Exact role override used by your template. Omit only when using its standard /LabRole fallback.')
    p.add_argument('--instance-profile',default='LabInstanceProfile')
    p.add_argument('--zones',nargs=2,default=['us-east-1a','us-east-1b'])
    a=p.parse_args();failures=[]
    def call(*args):
        result=subprocess.run(['aws',*args,'--region','us-east-1','--output','json','--no-cli-pager'],text=True,capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:600])
        return json.loads(result.stdout)
    def check(name,fn):
        try:
            info=fn()
            print('PASS:',name,info or '')
        except Exception as exc:
            failures.append(name)
            print('CHECK FAILED OR UNAVAILABLE:',name,'-',str(exc))
    try:
        account=call('sts','get-caller-identity')['Account']
        print('Current temporary AWS CLI account:',account)
    except Exception as exc:
        print('Cannot read current AWS CLI identity:',str(exc));return 2
    def profile():
        roles=call('iam','get-instance-profile','--instance-profile-name',a.instance_profile)['InstanceProfile']['Roles']
        expected=a.role_arn or 'arn:aws:iam::'+account+':role/LabRole'
        if expected not in [x['Arn'] for x in roles]:raise ValueError('Template role does not match this instance profile. Run prepare_lab.py to discover and fill the actual ARN, including any role path.')
        return a.instance_profile+' -> '+expected
    def certificate():
        if a.certificate_arn.split(':')[3:5]!=['us-east-1',account]:raise ValueError('Use a certificate in this account and us-east-1.')
        info=call('acm','describe-certificate','--certificate-arn',a.certificate_arn)['Certificate']
        if info['Status']!='ISSUED':raise ValueError('Certificate status is '+info['Status'])
        return info['DomainName']+'; verify hostname, trust and expiry separately'
    def zones():
        if a.zones[0]==a.zones[1]:raise ValueError('Two distinct zones are required.')
        items=call('ec2','describe-instance-type-offerings','--location-type','availability-zone','--filters','Name=instance-type,Values=t3.micro')['InstanceTypeOfferings']
        offered={x['Location'] for x in items}
        if not set(a.zones)<=offered:raise ValueError('Choose two zones offering t3.micro: '+', '.join(sorted(offered)))
        return ', '.join(a.zones)
    def database():
        versions=call('rds','describe-db-engine-versions','--engine','postgres')['DBEngineVersions']
        supported=[x['EngineVersion'] for x in versions if x['EngineVersion'].startswith('16.')]
        if not supported:raise ValueError('No PostgreSQL 16 minor version was returned.')
        options=call('rds','describe-orderable-db-instance-options','--engine','postgres','--db-instance-class','db.t3.micro')['OrderableDBInstanceOptions']
        valid=[x for x in options if x['EngineVersion'].startswith('16.') and x.get('MultiAZCapable')]
        if not valid:raise ValueError('No Multi-AZ PostgreSQL 16 db.t3.micro option returned; check permitted alternative classes.')
        return 'Multi-AZ db.t3.micro PostgreSQL 16; available minors include '+', '.join(supported[-5:])
    check('Existing profile matches the role ARN',profile)
    check('Certificate is issued in this account/region',certificate)
    check('Two zones offer the initial instance size',zones)
    check('Database engine/class offers Multi-AZ',database)
    print('\nThese are READ-ONLY checks. Check lab credits and service-creation permissions too.')
    print('AccessDenied on a read is inconclusive; consult the lab/tutor rather than changing shared IAM permissions.')
    return 1 if failures else 0

if __name__=='__main__':sys.exit(main())
