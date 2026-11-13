#!/usr/bin/env python3
"""Build the self-contained AWS Academy CloudFormation JSON from readable source.

Only files in src are embedded. Build, lint and local tests do not contact AWS.
"""
import base64
import io
import json
import lzma
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parent

def ref(name): return {'Ref':name}
def att(name,key): return {'Fn::GetAtt':[name,key]}
def sub(value): return {'Fn::Sub':value}
def tag(name=None):
    return [{'Key':'Project','Value':'INFS3701-Corporate-Gateway'}]+([{'Key':'Name','Value':name}] if name else [])

def make_bundle():
    required=['app.py','install.sh','requirements.txt','templates/index.html','templates/login.html']
    for name in required:
        if not (ROOT/'src'/name).is_file():raise SystemExit('Missing application source: '+name)
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w') as archive:
        for p in sorted((ROOT/'src').rglob('*')):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
            info=archive.gettarinfo(str(p),arcname=str(p.relative_to(ROOT/'src')))
            info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
            with p.open('rb') as stream:archive.addfile(info,stream)
    return base64.b64encode(lzma.compress(buf.getvalue(),preset=9)).decode()

def template(bundle):
    target_group_name={'Fn::Join':['',['gw-',{'Fn::Select':[0,{'Fn::Split':['-',{'Fn::Select':[2,{'Fn::Split':['/',ref('AWS::StackId')]}]}]}]}]]}
    t={
      'AWSTemplateFormatVersion':'2010-09-09',
      'Description':'INFS3701 Corporate Gateway: private two-AZ Flask, Multi-AZ PostgreSQL, private S3, HTTPS ALB, CloudWatch and SNS email. Run tools/prepare_lab.py for a ready-to-upload template. Reuses existing lab IAM only; no domain required for disclosed self-signed lab HTTPS.',
      'Metadata':{'AWS::CloudFormation::Interface':{'ParameterGroups':[
          {'Label':{'default':'Email and HTTPS certificate (pre-filled when prepared with tools/prepare_lab.py)'},'Parameters':['AlertEmail','CertificateArn']},
          {'Label':{'default':'Existing Learner Lab access and optional custom domain'},'Parameters':['ExistingInstanceProfile','ExistingRoleArn','ApplicationHostname']},
          {'Label':{'default':'Sizing and placement'},'Parameters':['ZoneA','ZoneB','InstanceType','DBInstanceClass','DBEngineVersion','AdminUsername','LogRetentionDays']}]}},
      'Parameters':{
        'ExistingInstanceProfile':{'Type':'String','Default':'LabInstanceProfile','AllowedPattern':'[A-Za-z0-9+=,.@_-]+','Description':'Existing EC2 instance profile; no IAM resources will be created.'},
        'ExistingRoleArn':{'Type':'String','Default':'','AllowedPattern':'^$|arn:aws:iam::[0-9]{12}:role/.+','Description':'Usually leave blank for the existing /LabRole in this account. This assumes the standard lab role exists; it does not create one. prepare_lab.py verifies the profile and fills its actual role ARN, including any path.'},
        'CertificateArn':{'Type':'String','AllowedPattern':'arn:aws:acm:us-east-1:[0-9]{12}:certificate/[A-Za-z0-9-]+','Description':'HTTPS requires a certificate. Run tools/prepare_lab.py to pre-fill a temporary self-signed lab certificate without a domain, or supply an existing matching issued ACM certificate. Self-signed certificates cause browser warnings.'},
        'ApplicationHostname':{'Type':'String','Default':'','AllowedPattern':'^$|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?','Description':'LEAVE BLANK if you have no domain. The output uses the AWS load-balancer address. With a self-signed lab certificate, expect a trust/name warning. No domain purchase is required for that lab-only route.'},
        'AlertEmail':{'Type':'String','AllowedPattern':'[^\\s@]+@[^\\s@]+\\.[^\\s@]+','Description':'Email for SNS alerts. You must click Confirm subscription in its email.'},
        'AdminUsername':{'Type':'String','Default':'admin','AllowedPattern':'[A-Za-z][A-Za-z0-9_-]{2,31}','Description':'Portal username. The generated login password is in ApplicationSecretArn.'},
        'ZoneA':{'Type':'AWS::EC2::AvailabilityZone::Name','Default':'us-east-1a','Description':'First zone; choose a zone supporting t3 instances.'},
        'ZoneB':{'Type':'AWS::EC2::AvailabilityZone::Name','Default':'us-east-1b','Description':'Second, different zone; choose one supporting t3 instances.'},
        'InstanceType':{'Type':'String','Default':'t3.micro','AllowedValues':['t3.micro','t3.small'],'Description':'One instance of this size is created in each zone.'},
        'DBInstanceClass':{'Type':'String','Default':'db.t3.micro','AllowedValues':['db.t3.micro','db.t3.small','db.t4g.micro','db.t4g.small'],'Description':'Small PostgreSQL 16 class, subject to lab permissions and regional support.'},
        'DBEngineVersion':{'Type':'String','Default':'16','AllowedPattern':'16(\\.[0-9]+)?','Description':'PostgreSQL major 16 (AWS selects the default minor), or an available 16.x version confirmed by preflight. Engine availability changes.'},
        'LogRetentionDays':{'Type':'Number','Default':1,'AllowedValues':[1,3,7,14],'Description':'Keep application logs for this many days. One day reduces retained lab logs; export evidence before it expires.'},
        'LatestAmiId':{'Type':'AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>','Default':'/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64','Description':'Latest Amazon Linux 2023 x86_64 image.'}
      },
      'Rules':{
        'DifferentZones':{'Assertions':[{'Assert':{'Fn::Not':[{'Fn::Equals':[ref('ZoneA'),ref('ZoneB')]}]},'AssertDescription':'Choose two different Availability Zones.'}]},
        'RegionRestriction':{'Assertions':[{'Assert':{'Fn::Equals':[ref('AWS::Region'),'us-east-1']},'AssertDescription':'Deploy in N. Virginia (us-east-1).'}]}
      },
      'Conditions':{'HasHostname':{'Fn::Not':[{'Fn::Equals':[ref('ApplicationHostname'),'']}]},'HasExistingRoleArn':{'Fn::Not':[{'Fn::Equals':[ref('ExistingRoleArn'),'']}]}} ,
      'Mappings':{'GatewayCode':{'Bundle':{'Data':bundle}}},
      'Resources':{},'Outputs':{}
    }
    r=t['Resources']
    existing_role={'Fn::If':['HasExistingRoleArn',ref('ExistingRoleArn'),sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:role/LabRole')]}
    def resource(name,typ,props,**extra):r[name]={'Type':typ,'Properties':props,**extra}
    resource('GatewayVPC','AWS::EC2::VPC',{'CidrBlock':'10.0.0.0/16','EnableDnsSupport':True,'EnableDnsHostnames':True,'Tags':tag('gateway-vpc')})
    resource('InternetGateway','AWS::EC2::InternetGateway',{'Tags':tag('gateway-igw')})
    resource('GatewayAttachment','AWS::EC2::VPCGatewayAttachment',{'VpcId':ref('GatewayVPC'),'InternetGatewayId':ref('InternetGateway')})
    resource('PublicRoutes','AWS::EC2::RouteTable',{'VpcId':ref('GatewayVPC'),'Tags':tag('gateway-public-routes')})
    resource('PublicDefaultRoute','AWS::EC2::Route',{'RouteTableId':ref('PublicRoutes'),'DestinationCidrBlock':'0.0.0.0/0','GatewayId':ref('InternetGateway')},DependsOn='GatewayAttachment')
    resource('DataRoutes','AWS::EC2::RouteTable',{'VpcId':ref('GatewayVPC'),'Tags':tag('gateway-isolated-data-routes')})
    for suffix,zone,octet in [('A','ZoneA',1),('B','ZoneB',2)]:
        for tier,offset,public in [('Public',0,True),('App',10,False),('Data',20,False)]:
            name=tier+'Subnet'+suffix
            resource(name,'AWS::EC2::Subnet',{'VpcId':ref('GatewayVPC'),'AvailabilityZone':ref(zone),'CidrBlock':f'10.0.{offset+octet}.0/24','MapPublicIpOnLaunch':public,'Tags':tag('gateway-'+tier.lower()+'-'+suffix.lower())})
        resource('NatEip'+suffix,'AWS::EC2::EIP',{'Domain':'vpc','Tags':tag('gateway-nat-ip-'+suffix.lower())},DependsOn='GatewayAttachment')
        resource('NatGateway'+suffix,'AWS::EC2::NatGateway',{'SubnetId':ref('PublicSubnet'+suffix),'AllocationId':att('NatEip'+suffix,'AllocationId'),'Tags':tag('gateway-nat-'+suffix.lower())})
        resource('AppRoutes'+suffix,'AWS::EC2::RouteTable',{'VpcId':ref('GatewayVPC'),'Tags':tag('gateway-app-routes-'+suffix.lower())})
        resource('AppDefaultRoute'+suffix,'AWS::EC2::Route',{'RouteTableId':ref('AppRoutes'+suffix),'DestinationCidrBlock':'0.0.0.0/0','NatGatewayId':ref('NatGateway'+suffix)})
        for tier,table in [('Public','PublicRoutes'),('App','AppRoutes'+suffix),('Data','DataRoutes')]:
            resource(tier+'RouteAssociation'+suffix,'AWS::EC2::SubnetRouteTableAssociation',{'SubnetId':ref(tier+'Subnet'+suffix),'RouteTableId':ref(table)})
    resource('S3Endpoint','AWS::EC2::VPCEndpoint',{'VpcId':ref('GatewayVPC'),'ServiceName':sub('com.amazonaws.${AWS::Region}.s3'),'VpcEndpointType':'Gateway','RouteTableIds':[ref('AppRoutesA'),ref('AppRoutesB')]})
    resource('AppSecurityGroup','AWS::EC2::SecurityGroup',{
        'VpcId':ref('GatewayVPC'),'GroupDescription':'Private app: HTTPS from ALB only. Outbound DNS, HTTPS, package HTTP, and database TLS.',
        'SecurityGroupEgress':[
            # Custom egress replaces the default allow-all rule, so DNS must be explicit
            # or package/API name resolution can fail during bootstrap.
            {'IpProtocol':'udp','FromPort':53,'ToPort':53,'CidrIp':'10.0.0.0/16','Description':'VPC Amazon DNS (UDP)'},
            {'IpProtocol':'tcp','FromPort':53,'ToPort':53,'CidrIp':'10.0.0.0/16','Description':'VPC Amazon DNS (TCP)'},
            {'IpProtocol':'udp','FromPort':53,'ToPort':53,'CidrIp':'169.254.169.253/32','Description':'Route 53 Resolver link-local DNS (UDP)'},
            {'IpProtocol':'tcp','FromPort':53,'ToPort':53,'CidrIp':'169.254.169.253/32','Description':'Route 53 Resolver link-local DNS (TCP)'},
            {'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIp':'0.0.0.0/0','Description':'AWS APIs, TLS and dependency downloads'},
            {'IpProtocol':'tcp','FromPort':80,'ToPort':80,'CidrIp':'0.0.0.0/0','Description':'OS repository mirrors during bootstrap'},
            {'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,'CidrIp':'10.0.21.0/24','Description':'PostgreSQL data subnet A'},
            {'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,'CidrIp':'10.0.22.0/24','Description':'PostgreSQL data subnet B'}],
        'Tags':tag('gateway-app-sg')})
    resource('AlbSecurityGroup','AWS::EC2::SecurityGroup',{
        'VpcId':ref('GatewayVPC'),'GroupDescription':'Public HTTPS entry and HTTP redirect; targets only on HTTPS.',
        'SecurityGroupIngress':[{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIp':'0.0.0.0/0','Description':'User HTTPS'},{'IpProtocol':'tcp','FromPort':80,'ToPort':80,'CidrIp':'0.0.0.0/0','Description':'Redirect to HTTPS'}],
        'SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':8443,'ToPort':8443,'DestinationSecurityGroupId':att('AppSecurityGroup','GroupId'),'Description':'Private HTTPS targets'}],
        'Tags':tag('gateway-alb-sg')})
    resource('AlbToAppIngress','AWS::EC2::SecurityGroupIngress',{'GroupId':att('AppSecurityGroup','GroupId'),'IpProtocol':'tcp','FromPort':8443,'ToPort':8443,'SourceSecurityGroupId':att('AlbSecurityGroup','GroupId'),'Description':'Only the ALB can reach application HTTPS'})
    resource('DatabaseSecurityGroup','AWS::EC2::SecurityGroup',{'VpcId':ref('GatewayVPC'),'GroupDescription':'PostgreSQL only from the gateway app tier.','SecurityGroupIngress':[{'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,'SourceSecurityGroupId':att('AppSecurityGroup','GroupId'),'Description':'PostgreSQL from private app servers'}],'Tags':tag('gateway-db-sg')})
    resource('FilesBucket','AWS::S3::Bucket',{
        'PublicAccessBlockConfiguration':{'BlockPublicAcls':True,'BlockPublicPolicy':True,'IgnorePublicAcls':True,'RestrictPublicBuckets':True},
        'OwnershipControls':{'Rules':[{'ObjectOwnership':'BucketOwnerEnforced'}]},
        'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]},
        'VersioningConfiguration':{'Status':'Enabled'},
        'LifecycleConfiguration':{'Rules':[{'Id':'AbortIncompleteUploads','Status':'Enabled','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]},
        'Tags':tag('gateway-files')},DeletionPolicy='Retain',UpdateReplacePolicy='Retain')
    resource('FilesBucketPolicy','AWS::S3::BucketPolicy',{'Bucket':ref('FilesBucket'),'PolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Sid':'ApplicationObjectAccess','Effect':'Allow','Principal':{'AWS':existing_role},'Action':['s3:GetObject','s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':sub('${FilesBucket.Arn}/assets/*')},
        {'Sid':'ApplicationListAssets','Effect':'Allow','Principal':{'AWS':existing_role},'Action':'s3:ListBucket','Resource':att('FilesBucket','Arn'),'Condition':{'StringLike':{'s3:prefix':['assets/','assets/*']}}},
        {'Sid':'RequireTLS','Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[att('FilesBucket','Arn'),sub('${FilesBucket.Arn}/*')],'Condition':{'Bool':{'aws:SecureTransport':'false','aws:PrincipalIsAWSService':'false'}}}
    ]}})
    resource('DatabaseAppSecret','AWS::SecretsManager::Secret',{'Description':'Generated credentials for restricted gateway PostgreSQL application user.','GenerateSecretString':{'SecretStringTemplate':'{"username":"gateway_app"}','GenerateStringKey':'password','PasswordLength':40,'ExcludePunctuation':True},'Tags':tag()})
    resource('ApplicationSecret','AWS::SecretsManager::Secret',{'Description':'Portal login. View this secret after deployment to retrieve the generated password.','GenerateSecretString':{'SecretStringTemplate':sub('{"username":"${AdminUsername}"}'),'GenerateStringKey':'password','PasswordLength':40,'ExcludePunctuation':True},'Tags':tag()})
    resource('DatabaseSubnets','AWS::RDS::DBSubnetGroup',{'DBSubnetGroupDescription':'Private data subnets in two different Availability Zones.','SubnetIds':[ref('DataSubnetA'),ref('DataSubnetB')],'Tags':tag()})
    resource('DatabaseParameters','AWS::RDS::DBParameterGroup',{'Description':'Gateway PostgreSQL 16 requires encrypted client connections.','Family':'postgres16','Parameters':{'rds.force_ssl':'1'},'Tags':tag()})
    resource('GatewayDatabase','AWS::RDS::DBInstance',{
        'Engine':'postgres','EngineVersion':ref('DBEngineVersion'),'DBParameterGroupName':ref('DatabaseParameters'),'DBInstanceClass':ref('DBInstanceClass'),'DBName':'gateway','MasterUsername':'gateway_admin','ManageMasterUserPassword':True,
        'AllocatedStorage':'20','StorageType':'gp3','StorageEncrypted':True,'MultiAZ':True,'PubliclyAccessible':False,'Port':'5432',
        'DBSubnetGroupName':ref('DatabaseSubnets'),'VPCSecurityGroups':[att('DatabaseSecurityGroup','GroupId')],'BackupRetentionPeriod':1,'DeleteAutomatedBackups':True,'DeletionProtection':False,
        'MonitoringInterval':0,'EnablePerformanceInsights':False,'AutoMinorVersionUpgrade':True,'CopyTagsToSnapshot':True,'Tags':tag('gateway-postgres')},DeletionPolicy='Delete',UpdateReplacePolicy='Delete')
    resource('AppLogs','AWS::Logs::LogGroup',{'LogGroupName':sub('/${AWS::StackName}/gateway'),'RetentionInDays':ref('LogRetentionDays'),'Tags':tag()})
    resource('AlertTopic','AWS::SNS::Topic',{'DisplayName':'Corporate Gateway alerts','TopicName':sub('${AWS::StackName}-alerts'),'Tags':tag()})
    resource('AlertTopicPolicy','AWS::SNS::TopicPolicy',{'Topics':[ref('AlertTopic')],'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Sid':'CloudWatchPublish','Effect':'Allow','Principal':{'Service':'cloudwatch.amazonaws.com'},'Action':'sns:Publish','Resource':ref('AlertTopic'),'Condition':{'StringEquals':{'aws:SourceAccount':ref('AWS::AccountId')},'ArnLike':{'aws:SourceArn':sub('arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:${AWS::StackName}-*')}}}]}})
    resource('EmailSubscription','AWS::SNS::Subscription',{'TopicArn':ref('AlertTopic'),'Protocol':'email','Endpoint':ref('AlertEmail')})
    resource('GatewayALB','AWS::ElasticLoadBalancingV2::LoadBalancer',{'Scheme':'internet-facing','Type':'application','IpAddressType':'ipv4','Subnets':[ref('PublicSubnetA'),ref('PublicSubnetB')],'SecurityGroups':[att('AlbSecurityGroup','GroupId')],'LoadBalancerAttributes':[{'Key':'idle_timeout.timeout_seconds','Value':'120'},{'Key':'routing.http.drop_invalid_header_fields.enabled','Value':'true'}],'Tags':tag('gateway-alb')},DependsOn=['GatewayAttachment','PublicDefaultRoute'])
    resource('GatewayTargets','AWS::ElasticLoadBalancingV2::TargetGroup',{'Name':target_group_name,'VpcId':ref('GatewayVPC'),'TargetType':'instance','Protocol':'HTTPS','Port':8443,'HealthCheckProtocol':'HTTPS','HealthCheckPort':'traffic-port','HealthCheckPath':'/health','HealthCheckIntervalSeconds':15,'HealthCheckTimeoutSeconds':5,'HealthyThresholdCount':2,'UnhealthyThresholdCount':2,'Matcher':{'HttpCode':'200'},'TargetGroupAttributes':[{'Key':'deregistration_delay.timeout_seconds','Value':'15'},{'Key':'stickiness.enabled','Value':'false'}],'Tags':tag('gateway-targets')})
    resource('HttpsListener','AWS::ElasticLoadBalancingV2::Listener',{'LoadBalancerArn':ref('GatewayALB'),'Protocol':'HTTPS','Port':443,'Certificates':[{'CertificateArn':ref('CertificateArn')}],'SslPolicy':'ELBSecurityPolicy-TLS13-1-2-2021-06','DefaultActions':[{'Type':'forward','TargetGroupArn':ref('GatewayTargets')}]})
    resource('HttpRedirect','AWS::ElasticLoadBalancingV2::Listener',{'LoadBalancerArn':ref('GatewayALB'),'Protocol':'HTTP','Port':80,'DefaultActions':[{'Type':'redirect','RedirectConfig':{'Protocol':'HTTPS','Port':'443','StatusCode':'HTTP_301'}}]})

    for suffix,zone in [('A','ZoneA'),('B','ZoneB')]:
        logical='AppServer'+suffix
        config={
            'region':'${AWS::Region}','db_host':'${GatewayDatabase.Endpoint.Address}','db_name':'gateway',
            'db_master_secret':'${MasterSecretArn}','db_app_secret':'${DatabaseAppSecret}',
            'app_secret':'${ApplicationSecret}','bucket':'${FilesBucket}','alb_arn':'${GatewayALB}',
            'target_group_name':'${TargetGroupName}','dashboard_name':'${AWS::StackName}-health',
            'topic_arn':'${AlertTopic}','stack_name':'${AWS::StackName}',
            'instance_name':'app-'+suffix.lower(),'zone':'${'+zone+'}',
            'alarm_name':'${AWS::StackName}-healthy-targets','log_group':'${AppLogs}',
            'certificate_ca':'/opt/gateway/rds-ca.pem'
        }
        opening='''#!/bin/bash
set -Eeuo pipefail
dnf install -y aws-cfn-bootstrap
signal_reason() {
  stage=$(cat /var/log/gateway-setup-stage 2>/dev/null || echo bootstrap)
  err=$(tr '\\n' ' ' </var/log/gateway-setup-error 2>/dev/null | cut -c1-120 || true)
  if test -n "$err"; then printf 'Setup exit %s; stage: %s; error: %s' "$1" "$stage" "$err"
  else printf 'Setup exit %s; stage: %s' "$1" "$stage"; fi
}
trap 'rc=$?; /opt/aws/bin/cfn-signal -e "$rc" --reason "$(signal_reason "$rc")" --stack "${AWS::StackName}" --resource "'''+logical+'''" --region "${AWS::Region}" || true' EXIT
install -d -m 750 /etc/gateway
mkdir -p /opt/gateway/src
/opt/aws/bin/cfn-init -v --stack "${AWS::StackName}" --resource "'''+logical+'''" --region "${AWS::Region}"
cat > /etc/gateway/runtime.json <<'GATEWAY_CONFIG'
'''+json.dumps(config,indent=2)+'''
GATEWAY_CONFIG
python3 -c "import base64,lzma,io,tarfile; data=lzma.decompress(base64.b64decode(open('/opt/gateway/bundle.b64').read())); tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt/gateway/src')"
'''
        closing='''
export GATEWAY_CONFIG=/etc/gateway/runtime.json
bash /opt/gateway/src/install.sh
'''
        userdata_vars={
            'TargetGroupName':target_group_name,
            'MasterSecretArn':{'Fn::GetAtt':['GatewayDatabase','MasterUserSecret.SecretArn']},
        }
        resource(logical,'AWS::EC2::Instance',{
            'ImageId':ref('LatestAmiId'),'InstanceType':ref('InstanceType'),'IamInstanceProfile':ref('ExistingInstanceProfile'),
            'Monitoring':False,
            'NetworkInterfaces':[{'DeviceIndex':'0','SubnetId':ref('AppSubnet'+suffix),'GroupSet':[att('AppSecurityGroup','GroupId')],'AssociatePublicIpAddress':False,'DeleteOnTermination':True}],
            'BlockDeviceMappings':[{'DeviceName':'/dev/xvda','Ebs':{'Encrypted':True,'VolumeSize':8,'VolumeType':'gp3','DeleteOnTermination':True}}],
            'MetadataOptions':{'HttpTokens':'required','HttpEndpoint':'enabled','HttpPutResponseHopLimit':1},
            'UserData':{'Fn::Base64':{'Fn::Join':['',[{'Fn::Sub':[opening,userdata_vars]},closing]]}},
            'Tags':tag('gateway-app-'+suffix.lower())
        },DependsOn=['AppDefaultRoute'+suffix,'AppRouteAssociation'+suffix,'S3Endpoint','FilesBucketPolicy','AlbToAppIngress'],CreationPolicy={'ResourceSignal':{'Count':1,'Timeout':'PT25M'}},Metadata={'AWS::CloudFormation::Init':{'config':{'files':{'/opt/gateway/bundle.b64':{'content':{'Fn::FindInMap':['GatewayCode','Bundle','Data']},'mode':'000600','owner':'root','group':'root'}}}}})
    # Runtime discovers this target group's ARN from the independently derived
    # target_group_name. The app bootstrap does not refer to the target resource,
    # avoiding the dependency cycle between Targets and app User Data.
    r['GatewayTargets']['Properties']['Targets']=[{'Id':ref('AppServerA'),'Port':8443},{'Id':ref('AppServerB'),'Port':8443}]
    dimensions=[{'Name':'LoadBalancer','Value':att('GatewayALB','LoadBalancerFullName')},{'Name':'TargetGroup','Value':att('GatewayTargets','TargetGroupFullName')}]
    resource('HealthyTargetsAlarm','AWS::CloudWatch::Alarm',{
        'AlarmName':sub('${AWS::StackName}-healthy-targets'),'AlarmDescription':'Fewer than two healthy Flask servers for two consecutive minutes; test by stopping ONE app server, then restart it.',
        'Namespace':'AWS/ApplicationELB','MetricName':'HealthyHostCount','Dimensions':dimensions,'Statistic':'Minimum','Period':60,'EvaluationPeriods':2,'DatapointsToAlarm':2,
        'Threshold':2,'ComparisonOperator':'LessThanThreshold','TreatMissingData':'breaching','AlarmActions':[ref('AlertTopic')],'OKActions':[ref('AlertTopic')]
    },DependsOn='AlertTopicPolicy')
    resource('TargetErrorsAlarm','AWS::CloudWatch::Alarm',{
        'AlarmName':sub('${AWS::StackName}-target-errors'),'AlarmDescription':'At least five target HTTP 5xx responses in one minute.',
        'Namespace':'AWS/ApplicationELB','MetricName':'HTTPCode_Target_5XX_Count','Dimensions':dimensions,'Statistic':'Sum','Period':60,'EvaluationPeriods':1,'DatapointsToAlarm':1,
        'Threshold':5,'ComparisonOperator':'GreaterThanOrEqualToThreshold','TreatMissingData':'notBreaching','AlarmActions':[ref('AlertTopic')],'OKActions':[ref('AlertTopic')]
    },DependsOn='AlertTopicPolicy')
    metrics_base=['AWS/ApplicationELB','HealthyHostCount','LoadBalancer','${GatewayALB.LoadBalancerFullName}','TargetGroup','${GatewayTargets.TargetGroupFullName}']
    widgets=[]
    def widget(title,metrics,x,y,w=12,h=6,stat='Average',unit=None):
        props={'title':title,'region':'${AWS::Region}','view':'timeSeries','stacked':False,'metrics':metrics,'period':60,'stat':stat}
        if unit:props['yAxis']={'left':{'label':unit,'showUnits':False}}
        widgets.append({'type':'metric','x':x,'y':y,'width':w,'height':h,'properties':props})
    widget('Healthy and unhealthy app servers',[metrics_base+[{'stat':'Minimum','label':'Healthy (minimum)'}],['AWS/ApplicationELB','UnHealthyHostCount','LoadBalancer','${GatewayALB.LoadBalancerFullName}','TargetGroup','${GatewayTargets.TargetGroupFullName}',{'stat':'Maximum','label':'Unhealthy (maximum)'}]],0,0)
    widget('Requests reaching the load balancer',[['AWS/ApplicationELB','RequestCount','LoadBalancer','${GatewayALB.LoadBalancerFullName}',{'stat':'Sum'}]],12,0)
    widget('Customer-visible server errors',[['AWS/ApplicationELB','HTTPCode_Target_5XX_Count','LoadBalancer','${GatewayALB.LoadBalancerFullName}','TargetGroup','${GatewayTargets.TargetGroupFullName}',{'stat':'Sum','label':'App target 5xx'}],['AWS/ApplicationELB','HTTPCode_ELB_5XX_Count','LoadBalancer','${GatewayALB.LoadBalancerFullName}',{'stat':'Sum','label':'Load balancer 5xx'}]],0,6)
    widget('Response time (95th percentile)',[['AWS/ApplicationELB','TargetResponseTime','LoadBalancer','${GatewayALB.LoadBalancerFullName}','TargetGroup','${GatewayTargets.TargetGroupFullName}',{'stat':'p95'}]],12,6)
    widget('EC2 CPU by app server',[['AWS/EC2','CPUUtilization','InstanceId','${AppServerA}',{'label':'App A'}],['AWS/EC2','CPUUtilization','InstanceId','${AppServerB}',{'label':'App B'}]],0,12)
    # Basic EC2 monitoring is five-minute, avoiding extra detailed-monitoring cost.
    widgets[-1]['properties']['period']=300
    widget('PostgreSQL CPU',[['AWS/RDS','CPUUtilization','DBInstanceIdentifier','${GatewayDatabase}']],12,12)
    widget('PostgreSQL database connections',[['AWS/RDS','DatabaseConnections','DBInstanceIdentifier','${GatewayDatabase}']],0,18)
    widget('PostgreSQL free storage',[['AWS/RDS','FreeStorageSpace','DBInstanceIdentifier','${GatewayDatabase}']],12,18)
    widgets.append({'type':'alarm','x':0,'y':24,'width':24,'height':3,'properties':{'title':'Email alert alarms','alarms':['${HealthyTargetsAlarm.Arn}','${TargetErrorsAlarm.Arn}']}})
    resource('HealthDashboard','AWS::CloudWatch::Dashboard',{'DashboardName':sub('${AWS::StackName}-health'),'DashboardBody':sub(json.dumps({'widgets':widgets},separators=(',',':')))})
    o=t['Outputs']
    def output(name,value,description):o[name]={'Value':value,'Description':description}
    output('ApplicationURL',{'Fn::If':['HasHostname',sub('https://${ApplicationHostname}'),sub('https://${GatewayALB.DNSName}')]},'Portal URL. A supplied hostname needs DNS pointing to the ALB. ALB DNS URL may not match your certificate.')
    output('LoadBalancerDNS',att('GatewayALB','DNSName'),'DNS target for your validated application hostname.')
    output('ExistingRoleUsed',existing_role,'Existing role named in the bucket policy. It must match the role in ExistingInstanceProfile. The template creates no IAM role.')
    output('CloudWatchDashboardURL',sub('https://${AWS::Region}.console.aws.amazon.com/cloudwatch/home?region=${AWS::Region}#dashboards:name=${AWS::StackName}-health'),'Dashboard with ALB, EC2, RDS and alarms.')
    output('DashboardName',sub('${AWS::StackName}-health'),'CloudWatch dashboard name.')
    for suffix in ['A','B']:output('AppServer'+suffix,ref('AppServer'+suffix),'Private app instance ID. Use EC2 Connect > Session Manager.')
    output('BucketName',ref('FilesBucket'),'Retained after stack deletion. Empty ALL object versions/delete markers, then delete the bucket manually after evidence is saved.')
    output('DatabaseIdentifier',ref('GatewayDatabase'),'Lab database. Stack deletion permanently deletes it without a final snapshot.')
    output('DatabaseEndpoint',att('GatewayDatabase','Endpoint.Address'),'Private PostgreSQL endpoint; connections must verify TLS.')
    output('ApplicationSecretArn',ref('ApplicationSecret'),'View this secret in Secrets Manager to retrieve the portal username/password. Never put the password in screenshots.')
    output('DatabaseAppSecretArn',ref('DatabaseAppSecret'),'Generated restricted PostgreSQL app-user credentials; not the portal password.')
    output('AlertTopicArn',ref('AlertTopic'),'SNS email topic. Confirm the emailed subscription before running alarm tests.')
    output('HealthyTargetsAlarmName',ref('HealthyTargetsAlarm'),'Main availability alarm: minimum healthy hosts <2 for two minutes.')
    output('TargetGroupArn',ref('GatewayTargets'),'ALB target group for target-health evidence.')
    output('LogGroupName',ref('AppLogs'),'Application, Nginx and bootstrap logs if permitted by the existing LabRole.')
    return t

def dependencies(obj):
    found=set()
    if isinstance(obj,dict):
        if 'Ref' in obj:found.add(obj['Ref'])
        if 'Fn::GetAtt' in obj:found.add(obj['Fn::GetAtt'][0])
        if 'Fn::Sub' in obj:
            import re
            value=obj['Fn::Sub'];value=value[0] if isinstance(value,list) else value
            found|={x.split('.')[0] for x in re.findall(r'\$\{([A-Za-z0-9_:.-]+)\}',value)}
        for v in obj.values():found|=dependencies(v)
    elif isinstance(obj,list):
        for v in obj:found|=dependencies(v)
    return found

def validate(t,bundle):
    resources=t['Resources'];graph={}
    for name,r in resources.items():
        d=r.get('DependsOn',[]);d=[d] if isinstance(d,str) else d
        graph[name]=(dependencies(r)|set(d))&set(resources)
    visited=set()
    def visit(name,path):
        if name in path:raise AssertionError('Resource cycle: '+' -> '.join(path+[name]))
        if name in visited:return
        for d in graph[name]:visit(d,path+[name])
        visited.add(name)
    for name in graph:visit(name,[])
    assert not any(r['Type'].startswith('AWS::IAM::') for r in resources.values())
    import re
    for suffix in ['A','B']:
        parts=resources['AppServer'+suffix]['Properties']['UserData']['Fn::Base64']['Fn::Join'][1]
        fragments=[]
        for p in parts:
            if isinstance(p,str):fragments.append(p)
            elif 'Fn::Sub' in p:
                value=p['Fn::Sub'];fragments.append(value[0] if isinstance(value,list) else value)
            else:fragments.append(bundle)
        script=''.join(fragments)
        # Conservative substitution budget for each dynamic ARN/hostname/value.
        script=re.sub(r'\$\{[^}]+\}',lambda m:'x'*256,script)
        length=len(script.encode())
        if length>16384:raise AssertionError(f'EC2 User Data too large: {length} bytes; reduce bundle/source.')
        print(f'AppServer{suffix}: conservative resolved User Data {length}/16384 bytes')
    print('PASS: no resource cycles, no IAM resources, User Data size limits.')

if __name__=='__main__':
    code=make_bundle();t=template(code);validate(t,code)
    path=ROOT/'gateway-stack.json';path.write_text(json.dumps(t,indent=2)+'\n')
    print(path, path.stat().st_size,'bytes; embedded compressed code',len(code),'characters')
