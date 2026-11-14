#!/usr/bin/env python3
"""Build CloudFormation JSON with S3-based app bootstrap (no embedded app in UserData).

UserData uses Fn::Join + Ref/GetAtt only — never Fn::Sub — so bash cannot break the template.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ref(name):
    return {'Ref': name}


def att(name, key):
    return {'Fn::GetAtt': [name, key]}


def tag(name=None):
    tags = [{'Key': 'Project', 'Value': 'INFS3701-Corporate-Gateway'}]
    if name:
        tags.append({'Key': 'Name', 'Value': name})
    return tags


def j(*parts):
    """Fn::Join with empty delimiter."""
    return {'Fn::Join': ['', list(parts)]}


def userdata(logical, zone_param, instance_name):
    """Tiny UserData: wait for net, pull bootstrap+app from S3, write runtime.json, run bootstrap."""
    # Build runtime.json with Join so DB endpoint/secrets resolve without Fn::Sub.
    runtime = j(
        '{\n',
        '  "region": "', ref('AWS::Region'), '",\n',
        '  "db_host": "', att('GatewayDatabase', 'Endpoint.Address'), '",\n',
        '  "db_name": "gateway",\n',
        '  "db_master_secret": "', att('GatewayDatabase', 'MasterUserSecret.SecretArn'), '",\n',
        '  "db_app_secret": "', ref('DatabaseAppSecret'), '",\n',
        '  "app_secret": "', ref('ApplicationSecret'), '",\n',
        '  "bucket": "', ref('FilesBucket'), '",\n',
        '  "alb_arn": "', ref('GatewayALB'), '",\n',
        '  "target_group_name": "', ref('GatewayTargets'), '",\n',
        '  "dashboard_name": "', ref('AWS::StackName'), '-health",\n',
        '  "topic_arn": "', ref('AlertTopic'), '",\n',
        '  "stack_name": "', ref('AWS::StackName'), '",\n',
        '  "instance_name": "', instance_name, '",\n',
        '  "zone": "', ref(zone_param), '",\n',
        '  "alarm_name": "', ref('AWS::StackName'), '-healthy-targets",\n',
        '  "log_group": "', ref('AppLogs'), '",\n',
        '  "certificate_ca": "/opt/gateway/rds-ca.pem"\n',
        '}\n',
    )
    # NOTE: target_group_name in app expects the NAME not ARN. Fix: use a dedicated name param.
    # We'll set GatewayTargets Name via a predictable join and pass that string instead.
    return {
        'Fn::Base64': j(
            '#!/bin/bash\n',
            'set -euxo pipefail\n',
            'exec > /var/log/gateway-userdata.log 2>&1\n',
            'echo userdata-start > /var/log/gateway-setup-stage\n',
            'echo wait-network > /var/log/gateway-setup-stage\n',
            'ok=0\n',
            'for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36; do\n',
            '  if curl -fsI --max-time 5 https://aws.amazon.com >/dev/null; then ok=1; break; fi\n',
            '  sleep 5\n',
            'done\n',
            'test "$ok" -eq 1\n',
            'echo install-tools > /var/log/gateway-setup-stage\n',
            'dnf install -y aws-cfn-bootstrap curl unzip awscli || dnf install -y aws-cfn-bootstrap curl unzip\n',
            'echo download-bootstrap > /var/log/gateway-setup-stage\n',
            'aws s3 cp s3://', ref('BootstrapBucket'), '/', ref('BootstrapPrefix'),
            'bootstrap.sh /tmp/bootstrap.sh --region ', ref('AWS::Region'), '\n',
            'aws s3 cp s3://', ref('BootstrapBucket'), '/', ref('BootstrapPrefix'),
            'app.tgz /tmp/gateway-app.tgz --region ', ref('AWS::Region'), '\n',
            'chmod +x /tmp/bootstrap.sh\n',
            'install -d -m 750 /etc/gateway\n',
            'echo write-config > /var/log/gateway-setup-stage\n',
            'cat > /etc/gateway/runtime.json << "EOF"\n',
            runtime,
            'EOF\n',
            'echo run-bootstrap > /var/log/gateway-setup-stage\n',
            'bash /tmp/bootstrap.sh ', ref('AWS::StackName'), ' ', logical, ' ', ref('AWS::Region'), '\n',
        )
    }


def target_group_name():
    # Short unique name from stack id (same idea as before) via Join — app discovers by name.
    return {
        'Fn::Join': [
            '',
            [
                'gw-',
                {'Fn::Select': [0, {'Fn::Split': ['-', {'Fn::Select': [2, {'Fn::Split': ['/', ref('AWS::StackId')]}]}]}]},
            ],
        ]
    }


def build():
    tg_name = target_group_name()
    t = {
        'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'INFS3701 Gateway S3-bootstrap: app code pulled from BootstrapBucket. UserData has no Fn::Sub.',
        'Metadata': {
            'AWS::CloudFormation::Interface': {
                'ParameterGroups': [
                    {'Label': {'default': '1) Email'}, 'Parameters': ['AlertEmail']},
                    {'Label': {'default': '2) HTTPS cert + lab IAM (prepare fills these)'}, 'Parameters': ['CertificateArn', 'ExistingInstanceProfile', 'ExistingRoleArn']},
                    {'Label': {'default': '3) Bootstrap S3 (prepare fills these)'}, 'Parameters': ['BootstrapBucket', 'BootstrapPrefix']},
                    {'Label': {'default': '4) Optional sizing'}, 'Parameters': ['ZoneA', 'ZoneB', 'InstanceType', 'DBInstanceClass', 'AdminUsername']},
                ]
            }
        },
        'Parameters': {
            'ExistingInstanceProfile': {
                'Type': 'String', 'Default': 'LabInstanceProfile',
                'AllowedPattern': '[A-Za-z0-9+=,.@_-]+',
                'Description': 'Existing EC2 instance profile (LabInstanceProfile).',
            },
            'ExistingRoleArn': {
                'Type': 'String', 'Default': '',
                'AllowedPattern': '^$|arn:aws:iam::[0-9]{12}:role/.+',
                'Description': 'LabRole ARN. prepare fills this.',
            },
            'CertificateArn': {
                'Type': 'String',
                'AllowedPattern': 'arn:aws:acm:us-east-1:[0-9]{12}:certificate/[A-Za-z0-9-]+',
                'Description': 'ACM certificate ARN (self-signed lab cert is fine).',
            },
            'AlertEmail': {
                'Type': 'String',
                'AllowedPattern': '[^\\s@]+@[^\\s@]+\\.[^\\s@]+',
                'Description': 'SNS alert email — confirm subscription after create.',
            },
            'AdminUsername': {
                'Type': 'String', 'Default': 'admin',
                'AllowedPattern': '[A-Za-z][A-Za-z0-9_-]{2,31}',
            },
            'BootstrapBucket': {
                'Type': 'String',
                'AllowedPattern': '[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',
                'Description': 'Bucket where prepare uploaded bootstrap.sh and app.tgz.',
            },
            'BootstrapPrefix': {
                'Type': 'String', 'Default': 'gateway/',
                'AllowedPattern': '^[a-zA-Z0-9!_.*\'()/+=:@,-]*/$',
                'Description': 'Key prefix ending with /',
            },
            'ZoneA': {'Type': 'AWS::EC2::AvailabilityZone::Name', 'Default': 'us-east-1a'},
            'ZoneB': {'Type': 'AWS::EC2::AvailabilityZone::Name', 'Default': 'us-east-1b'},
            'InstanceType': {'Type': 'String', 'Default': 't3.micro', 'AllowedValues': ['t3.micro', 't3.small']},
            'DBInstanceClass': {
                'Type': 'String', 'Default': 'db.t3.micro',
                'AllowedValues': ['db.t3.micro', 'db.t3.small'],
            },
            'DBEngineVersion': {'Type': 'String', 'Default': '16', 'AllowedPattern': '16(\\.[0-9]+)?'},
            'LatestAmiId': {
                'Type': 'AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>',
                'Default': '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64',
            },
        },
        'Rules': {
            'DifferentZones': {
                'Assertions': [{
                    'Assert': {'Fn::Not': [{'Fn::Equals': [ref('ZoneA'), ref('ZoneB')]}]},
                    'AssertDescription': 'Choose two different AZs.',
                }]
            },
            'RegionRestriction': {
                'Assertions': [{
                    'Assert': {'Fn::Equals': [ref('AWS::Region'), 'us-east-1']},
                    'AssertDescription': 'Deploy in us-east-1.',
                }]
            },
        },
        'Conditions': {
            'HasExistingRoleArn': {'Fn::Not': [{'Fn::Equals': [ref('ExistingRoleArn'), '']}]},
        },
        'Resources': {},
        'Outputs': {},
    }
    r = t['Resources']
    existing_role = {
        'Fn::If': [
            'HasExistingRoleArn',
            ref('ExistingRoleArn'),
            {'Fn::Join': ['', ['arn:aws:iam::', ref('AWS::AccountId'), ':role/LabRole']]},
        ]
    }

    def resource(name, typ, props, **extra):
        r[name] = {'Type': typ, 'Properties': props, **extra}

    # --- Network (1 NAT) ---
    resource('GatewayVPC', 'AWS::EC2::VPC', {
        'CidrBlock': '10.0.0.0/16', 'EnableDnsSupport': True, 'EnableDnsHostnames': True, 'Tags': tag('gateway-vpc'),
    })
    resource('InternetGateway', 'AWS::EC2::InternetGateway', {'Tags': tag('gateway-igw')})
    resource('GatewayAttachment', 'AWS::EC2::VPCGatewayAttachment', {
        'VpcId': ref('GatewayVPC'), 'InternetGatewayId': ref('InternetGateway'),
    })
    resource('PublicRoutes', 'AWS::EC2::RouteTable', {'VpcId': ref('GatewayVPC'), 'Tags': tag('gateway-public-routes')})
    resource('PublicDefaultRoute', 'AWS::EC2::Route', {
        'RouteTableId': ref('PublicRoutes'), 'DestinationCidrBlock': '0.0.0.0/0', 'GatewayId': ref('InternetGateway'),
    }, DependsOn='GatewayAttachment')
    resource('DataRoutes', 'AWS::EC2::RouteTable', {'VpcId': ref('GatewayVPC'), 'Tags': tag('gateway-data-routes')})
    resource('NatEipA', 'AWS::EC2::EIP', {'Domain': 'vpc', 'Tags': tag('gateway-nat-ip')}, DependsOn='GatewayAttachment')

    for suffix, zone, octet in [('A', 'ZoneA', 1), ('B', 'ZoneB', 2)]:
        for tier, offset, public in [('Public', 0, True), ('App', 10, False), ('Data', 20, False)]:
            resource(tier + 'Subnet' + suffix, 'AWS::EC2::Subnet', {
                'VpcId': ref('GatewayVPC'), 'AvailabilityZone': ref(zone),
                'CidrBlock': f'10.0.{offset + octet}.0/24', 'MapPublicIpOnLaunch': public,
                'Tags': tag(f'gateway-{tier.lower()}-{suffix.lower()}'),
            })
        resource('AppRoutes' + suffix, 'AWS::EC2::RouteTable', {
            'VpcId': ref('GatewayVPC'), 'Tags': tag(f'gateway-app-routes-{suffix.lower()}'),
        })
        for tier, table in [('Public', 'PublicRoutes'), ('App', 'AppRoutes' + suffix), ('Data', 'DataRoutes')]:
            resource(tier + 'RouteAssociation' + suffix, 'AWS::EC2::SubnetRouteTableAssociation', {
                'SubnetId': ref(tier + 'Subnet' + suffix), 'RouteTableId': ref(table),
            })

    resource('NatGatewayA', 'AWS::EC2::NatGateway', {
        'SubnetId': ref('PublicSubnetA'), 'AllocationId': att('NatEipA', 'AllocationId'), 'Tags': tag('gateway-nat'),
    })
    resource('AppDefaultRouteA', 'AWS::EC2::Route', {
        'RouteTableId': ref('AppRoutesA'), 'DestinationCidrBlock': '0.0.0.0/0', 'NatGatewayId': ref('NatGatewayA'),
    })
    resource('AppDefaultRouteB', 'AWS::EC2::Route', {
        'RouteTableId': ref('AppRoutesB'), 'DestinationCidrBlock': '0.0.0.0/0', 'NatGatewayId': ref('NatGatewayA'),
    })
    resource('S3Endpoint', 'AWS::EC2::VPCEndpoint', {
        'VpcId': ref('GatewayVPC'),
        'ServiceName': {'Fn::Join': ['', ['com.amazonaws.', ref('AWS::Region'), '.s3']]},
        'VpcEndpointType': 'Gateway',
        'RouteTableIds': [ref('AppRoutesA'), ref('AppRoutesB')],
    })

    # Default outbound (no custom egress) for reliable bootstrap.
    resource('AppSecurityGroup', 'AWS::EC2::SecurityGroup', {
        'VpcId': ref('GatewayVPC'),
        'GroupDescription': 'Private app; ALB HTTPS only inbound.',
        'Tags': tag('gateway-app-sg'),
    })
    resource('AlbSecurityGroup', 'AWS::EC2::SecurityGroup', {
        'VpcId': ref('GatewayVPC'),
        'GroupDescription': 'Public ALB 80/443',
        'SecurityGroupIngress': [
            {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443, 'CidrIp': '0.0.0.0/0'},
            {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'CidrIp': '0.0.0.0/0'},
        ],
        'SecurityGroupEgress': [
            {'IpProtocol': 'tcp', 'FromPort': 8443, 'ToPort': 8443, 'DestinationSecurityGroupId': att('AppSecurityGroup', 'GroupId')},
        ],
        'Tags': tag('gateway-alb-sg'),
    })
    resource('AlbToAppIngress', 'AWS::EC2::SecurityGroupIngress', {
        'GroupId': att('AppSecurityGroup', 'GroupId'), 'IpProtocol': 'tcp',
        'FromPort': 8443, 'ToPort': 8443, 'SourceSecurityGroupId': att('AlbSecurityGroup', 'GroupId'),
    })
    resource('DatabaseSecurityGroup', 'AWS::EC2::SecurityGroup', {
        'VpcId': ref('GatewayVPC'),
        'GroupDescription': 'Postgres from app only',
        'SecurityGroupIngress': [
            {'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432, 'SourceSecurityGroupId': att('AppSecurityGroup', 'GroupId')},
        ],
        'Tags': tag('gateway-db-sg'),
    })

    resource('FilesBucket', 'AWS::S3::Bucket', {
        'PublicAccessBlockConfiguration': {
            'BlockPublicAcls': True, 'BlockPublicPolicy': True, 'IgnorePublicAcls': True, 'RestrictPublicBuckets': True,
        },
        'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]},
        'BucketEncryption': {'ServerSideEncryptionConfiguration': [{'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]},
        'VersioningConfiguration': {'Status': 'Enabled'},
        'Tags': tag('gateway-files'),
    }, DeletionPolicy='Retain', UpdateReplacePolicy='Retain')
    resource('FilesBucketPolicy', 'AWS::S3::BucketPolicy', {
        'Bucket': ref('FilesBucket'),
        'PolicyDocument': {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Sid': 'AppObjects', 'Effect': 'Allow', 'Principal': {'AWS': existing_role},
                    'Action': ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
                    'Resource': {'Fn::Join': ['', [att('FilesBucket', 'Arn'), '/assets/*']]},
                },
                {
                    'Sid': 'AppList', 'Effect': 'Allow', 'Principal': {'AWS': existing_role},
                    'Action': 's3:ListBucket', 'Resource': att('FilesBucket', 'Arn'),
                    'Condition': {'StringLike': {'s3:prefix': ['assets/', 'assets/*']}},
                },
                {
                    'Sid': 'DenyInsecure', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
                    'Resource': [att('FilesBucket', 'Arn'), {'Fn::Join': ['', [att('FilesBucket', 'Arn'), '/*']]}],
                    'Condition': {'Bool': {'aws:SecureTransport': 'false', 'aws:PrincipalIsAWSService': 'false'}},
                },
            ],
        },
    })

    resource('DatabaseAppSecret', 'AWS::SecretsManager::Secret', {
        'Description': 'App DB user',
        'GenerateSecretString': {
            'SecretStringTemplate': '{"username":"gateway_app"}',
            'GenerateStringKey': 'password', 'PasswordLength': 40, 'ExcludePunctuation': True,
        },
        'Tags': tag(),
    })
    resource('ApplicationSecret', 'AWS::SecretsManager::Secret', {
        'Description': 'Portal login',
        'GenerateSecretString': {
            'SecretStringTemplate': {'Fn::Join': ['', ['{"username":"', ref('AdminUsername'), '"}']]},
            'GenerateStringKey': 'password', 'PasswordLength': 40, 'ExcludePunctuation': True,
        },
        'Tags': tag(),
    })
    resource('DatabaseSubnets', 'AWS::RDS::DBSubnetGroup', {
        'DBSubnetGroupDescription': 'Private data subnets',
        'SubnetIds': [ref('DataSubnetA'), ref('DataSubnetB')], 'Tags': tag(),
    })
    resource('DatabaseParameters', 'AWS::RDS::DBParameterGroup', {
        'Description': 'Force SSL', 'Family': 'postgres16',
        'Parameters': {'rds.force_ssl': '1'}, 'Tags': tag(),
    })
    resource('GatewayDatabase', 'AWS::RDS::DBInstance', {
        'Engine': 'postgres', 'EngineVersion': ref('DBEngineVersion'),
        'DBParameterGroupName': ref('DatabaseParameters'),
        'DBInstanceClass': ref('DBInstanceClass'), 'DBName': 'gateway',
        'MasterUsername': 'gateway_admin', 'ManageMasterUserPassword': True,
        'AllocatedStorage': '20', 'StorageType': 'gp3', 'StorageEncrypted': True,
        'MultiAZ': True, 'PubliclyAccessible': False, 'Port': '5432',
        'DBSubnetGroupName': ref('DatabaseSubnets'),
        'VPCSecurityGroups': [att('DatabaseSecurityGroup', 'GroupId')],
        'BackupRetentionPeriod': 1, 'DeleteAutomatedBackups': True, 'DeletionProtection': False,
        'MonitoringInterval': 0, 'EnablePerformanceInsights': False,
        'AutoMinorVersionUpgrade': True, 'Tags': tag('gateway-postgres'),
    }, DeletionPolicy='Delete', UpdateReplacePolicy='Delete')

    resource('AppLogs', 'AWS::Logs::LogGroup', {
        'LogGroupName': {'Fn::Join': ['', ['/', ref('AWS::StackName'), '/gateway']]},
        'RetentionInDays': 1, 'Tags': tag(),
    })
    resource('AlertTopic', 'AWS::SNS::Topic', {
        'DisplayName': 'Gateway alerts',
        'TopicName': {'Fn::Join': ['', [ref('AWS::StackName'), '-alerts']]},
        'Tags': tag(),
    })
    resource('AlertTopicPolicy', 'AWS::SNS::TopicPolicy', {
        'Topics': [ref('AlertTopic')],
        'PolicyDocument': {
            'Version': '2012-10-17',
            'Statement': [{
                'Sid': 'CW', 'Effect': 'Allow', 'Principal': {'Service': 'cloudwatch.amazonaws.com'},
                'Action': 'sns:Publish', 'Resource': ref('AlertTopic'),
                'Condition': {
                    'StringEquals': {'aws:SourceAccount': ref('AWS::AccountId')},
                    'ArnLike': {
                        'aws:SourceArn': {
                            'Fn::Join': ['', [
                                'arn:aws:cloudwatch:', ref('AWS::Region'), ':', ref('AWS::AccountId'),
                                ':alarm:', ref('AWS::StackName'), '-*',
                            ]],
                        }
                    },
                },
            }],
        },
    })
    resource('EmailSubscription', 'AWS::SNS::Subscription', {
        'TopicArn': ref('AlertTopic'), 'Protocol': 'email', 'Endpoint': ref('AlertEmail'),
    })

    resource('GatewayALB', 'AWS::ElasticLoadBalancingV2::LoadBalancer', {
        'Scheme': 'internet-facing', 'Type': 'application', 'IpAddressType': 'ipv4',
        'Subnets': [ref('PublicSubnetA'), ref('PublicSubnetB')],
        'SecurityGroups': [att('AlbSecurityGroup', 'GroupId')],
        'Tags': tag('gateway-alb'),
    }, DependsOn=['GatewayAttachment', 'PublicDefaultRoute'])
    resource('GatewayTargets', 'AWS::ElasticLoadBalancingV2::TargetGroup', {
        'Name': tg_name, 'VpcId': ref('GatewayVPC'), 'TargetType': 'instance',
        'Protocol': 'HTTPS', 'Port': 8443,
        'HealthCheckProtocol': 'HTTPS', 'HealthCheckPath': '/health',
        'HealthCheckIntervalSeconds': 15, 'HealthCheckTimeoutSeconds': 5,
        'HealthyThresholdCount': 2, 'UnhealthyThresholdCount': 2,
        'Matcher': {'HttpCode': '200'},
        'TargetGroupAttributes': [
            {'Key': 'deregistration_delay.timeout_seconds', 'Value': '15'},
            {'Key': 'stickiness.enabled', 'Value': 'false'},
        ],
        'Tags': tag('gateway-targets'),
    })
    resource('HttpsListener', 'AWS::ElasticLoadBalancingV2::Listener', {
        'LoadBalancerArn': ref('GatewayALB'), 'Protocol': 'HTTPS', 'Port': 443,
        'Certificates': [{'CertificateArn': ref('CertificateArn')}],
        'SslPolicy': 'ELBSecurityPolicy-TLS13-1-2-2021-06',
        'DefaultActions': [{'Type': 'forward', 'TargetGroupArn': ref('GatewayTargets')}],
    })
    resource('HttpRedirect', 'AWS::ElasticLoadBalancingV2::Listener', {
        'LoadBalancerArn': ref('GatewayALB'), 'Protocol': 'HTTP', 'Port': 80,
        'DefaultActions': [{
            'Type': 'redirect',
            'RedirectConfig': {'Protocol': 'HTTPS', 'Port': '443', 'StatusCode': 'HTTP_301'},
        }],
    })

    def userdata_fixed(logical, zone_param, instance_name):
        # Flat Join only (no nested Join objects). Install cfn-signal FIRST so hangs
        # after that still report CREATE_FAILED with a reason instead of a silent timeout.
        return {
            'Fn::Base64': j(
                '#!/bin/bash\n',
                'set -ux\n',
                'exec > /var/log/gateway-userdata.log 2>&1\n',
                'echo userdata-start > /var/log/gateway-setup-stage\n',
                'echo wait-network > /var/log/gateway-setup-stage\n',
                'ok=0\n',
                'for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do\n',
                '  if command -v curl >/dev/null && curl -fsI --max-time 5 https://aws.amazon.com >/dev/null; then ok=1; break; fi\n',
                '  sleep 5\n',
                'done\n',
                'echo install-tools > /var/log/gateway-setup-stage\n',
                'timeout 240 dnf install -y aws-cfn-bootstrap curl unzip || dnf install -y aws-cfn-bootstrap curl unzip\n',
                'test -x /opt/aws/bin/cfn-signal\n',
                'trap \'/opt/aws/bin/cfn-signal -e $? --reason userdata-failed --stack ',
                ref('AWS::StackName'), ' --resource ', logical, ' --region ', ref('AWS::Region'), ' || true\' EXIT\n',
                'test "$ok" -eq 1\n',
                'command -v aws >/dev/null || timeout 180 dnf install -y awscli\n',
                'echo download-bootstrap > /var/log/gateway-setup-stage\n',
                'aws s3 cp s3://', ref('BootstrapBucket'), '/', ref('BootstrapPrefix'),
                'bootstrap.sh /tmp/bootstrap.sh --region ', ref('AWS::Region'), '\n',
                'aws s3 cp s3://', ref('BootstrapBucket'), '/', ref('BootstrapPrefix'),
                'app.tgz /tmp/gateway-app.tgz --region ', ref('AWS::Region'), '\n',
                'chmod +x /tmp/bootstrap.sh\n',
                'install -d -m 750 /etc/gateway\n',
                'echo write-config > /var/log/gateway-setup-stage\n',
                'cat > /etc/gateway/runtime.json << "EOF"\n',
                '{\n',
                '  "region": "', ref('AWS::Region'), '",\n',
                '  "db_host": "', att('GatewayDatabase', 'Endpoint.Address'), '",\n',
                '  "db_name": "gateway",\n',
                '  "db_master_secret": "', att('GatewayDatabase', 'MasterUserSecret.SecretArn'), '",\n',
                '  "db_app_secret": "', ref('DatabaseAppSecret'), '",\n',
                '  "app_secret": "', ref('ApplicationSecret'), '",\n',
                '  "bucket": "', ref('FilesBucket'), '",\n',
                '  "alb_arn": "', ref('GatewayALB'), '",\n',
                '  "target_group_name": "', tg_name, '",\n',
                '  "dashboard_name": "', ref('AWS::StackName'), '-health",\n',
                '  "topic_arn": "', ref('AlertTopic'), '",\n',
                '  "stack_name": "', ref('AWS::StackName'), '",\n',
                '  "instance_name": "', instance_name, '",\n',
                '  "zone": "', ref(zone_param), '",\n',
                '  "alarm_name": "', ref('AWS::StackName'), '-healthy-targets",\n',
                '  "log_group": "', ref('AppLogs'), '",\n',
                '  "certificate_ca": "/opt/gateway/rds-ca.pem"\n',
                '}\n',
                'EOF\n',
                'echo run-bootstrap > /var/log/gateway-setup-stage\n',
                'trap - EXIT\n',
                'bash /tmp/bootstrap.sh ', ref('AWS::StackName'), ' ', logical, ' ', ref('AWS::Region'), '\n',
            )
        }

    for suffix, zone, name in [('A', 'ZoneA', 'app-a'), ('B', 'ZoneB', 'app-b')]:
        logical = 'AppServer' + suffix
        resource(logical, 'AWS::EC2::Instance', {
            'ImageId': ref('LatestAmiId'),
            'InstanceType': ref('InstanceType'),
            'IamInstanceProfile': ref('ExistingInstanceProfile'),
            'Monitoring': False,
            'NetworkInterfaces': [{
                'DeviceIndex': '0',
                'SubnetId': ref('AppSubnet' + suffix),
                'GroupSet': [att('AppSecurityGroup', 'GroupId')],
                'AssociatePublicIpAddress': False,
                'DeleteOnTermination': True,
            }],
            'BlockDeviceMappings': [{
                'DeviceName': '/dev/xvda',
                'Ebs': {'Encrypted': True, 'VolumeSize': 8, 'VolumeType': 'gp3', 'DeleteOnTermination': True},
            }],
            'MetadataOptions': {'HttpTokens': 'required', 'HttpEndpoint': 'enabled', 'HttpPutResponseHopLimit': 1},
            'UserData': userdata_fixed(logical, zone, name),
            'Tags': tag('gateway-app-' + suffix.lower()),
        }, DependsOn=[
            'NatGatewayA', 'AppDefaultRoute' + suffix, 'AppRouteAssociation' + suffix,
            'S3Endpoint', 'FilesBucketPolicy', 'AlbToAppIngress',
        ], CreationPolicy={'ResourceSignal': {'Count': 1, 'Timeout': 'PT50M'}})

    r['GatewayTargets']['Properties']['Targets'] = [
        {'Id': ref('AppServerA'), 'Port': 8443},
        {'Id': ref('AppServerB'), 'Port': 8443},
    ]

    dims = [
        {'Name': 'LoadBalancer', 'Value': att('GatewayALB', 'LoadBalancerFullName')},
        {'Name': 'TargetGroup', 'Value': att('GatewayTargets', 'TargetGroupFullName')},
    ]
    resource('HealthyTargetsAlarm', 'AWS::CloudWatch::Alarm', {
        'AlarmName': {'Fn::Join': ['', [ref('AWS::StackName'), '-healthy-targets']]},
        'AlarmDescription': 'HealthyHostCount minimum < 2 for 2 minutes',
        'Namespace': 'AWS/ApplicationELB', 'MetricName': 'HealthyHostCount',
        'Dimensions': dims, 'Statistic': 'Minimum', 'Period': 60,
        'EvaluationPeriods': 2, 'DatapointsToAlarm': 2, 'Threshold': 2,
        'ComparisonOperator': 'LessThanThreshold', 'TreatMissingData': 'breaching',
        'AlarmActions': [ref('AlertTopic')], 'OKActions': [ref('AlertTopic')],
    }, DependsOn='AlertTopicPolicy')

    # Minimal dashboard body without Fn::Sub — use Join for region/names where needed.
    dash_body = {
        'Fn::Join': ['', [
            '{"widgets":[{"type":"metric","x":0,"y":0,"width":24,"height":6,"properties":{"title":"Healthy hosts","region":"',
            ref('AWS::Region'),
            '","metrics":[["AWS/ApplicationELB","HealthyHostCount","LoadBalancer","',
            att('GatewayALB', 'LoadBalancerFullName'),
            '","TargetGroup","',
            att('GatewayTargets', 'TargetGroupFullName'),
            '",{"stat":"Minimum"}]],"period":60,"stat":"Average","view":"timeSeries"}}]}',
        ]]
    }
    resource('HealthDashboard', 'AWS::CloudWatch::Dashboard', {
        'DashboardName': {'Fn::Join': ['', [ref('AWS::StackName'), '-health']]},
        'DashboardBody': dash_body,
    })

    o = t['Outputs']

    def output(name, value, description):
        o[name] = {'Value': value, 'Description': description}

    output('ApplicationURL', {'Fn::Join': ['', ['https://', att('GatewayALB', 'DNSName')]]}, 'Portal URL (cert warning expected with lab cert)')
    output('LoadBalancerDNS', att('GatewayALB', 'DNSName'), 'ALB DNS')
    output('CloudWatchDashboardURL', {
        'Fn::Join': ['', [
            'https://', ref('AWS::Region'),
            '.console.aws.amazon.com/cloudwatch/home?region=', ref('AWS::Region'),
            '#dashboards:name=', ref('AWS::StackName'), '-health',
        ]]
    }, 'Dashboard')
    output('AppServerA', ref('AppServerA'), 'Instance A')
    output('AppServerB', ref('AppServerB'), 'Instance B')
    output('BucketName', ref('FilesBucket'), 'Upload bucket (retained on delete)')
    output('ApplicationSecretArn', ref('ApplicationSecret'), 'Portal login secret')
    output('AlertTopicArn', ref('AlertTopic'), 'SNS topic')
    output('HealthyTargetsAlarmName', ref('HealthyTargetsAlarm'), 'Main alarm')
    output('BootstrapBucketUsed', ref('BootstrapBucket'), 'Bootstrap code bucket')
    return t


def assert_no_fn_sub(obj, path='root'):
    if isinstance(obj, dict):
        if 'Fn::Sub' in obj:
            raise AssertionError('Fn::Sub not allowed in S3-bootstrap template at ' + path)
        for k, v in obj.items():
            assert_no_fn_sub(v, path + '.' + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_no_fn_sub(v, f'{path}[{i}]')


if __name__ == '__main__':
    template = build()
    assert_no_fn_sub(template)
    assert not any(r['Type'].startswith('AWS::IAM::') for r in template['Resources'].values())
    path = ROOT / 'gateway-stack.json'
    path.write_text(json.dumps(template, indent=2) + '\n')
    print('Wrote', path, 'resources=', len(template['Resources']), 'no Fn::Sub OK')
