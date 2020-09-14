import argparse
import configparser
import itertools
import collections
import json
import sys

import boto3

import yaml

def represent_ordereddict(dumper, data):
    value = []

    for item_key, item_value in data.items():
        node_key = dumper.represent_data(item_key)
        node_value = dumper.represent_data(item_value)

        value.append((node_key, node_value))

    return yaml.nodes.MappingNode(u'tag:yaml.org,2002:map', value)

yaml.add_representer(collections.OrderedDict, represent_ordereddict)
yaml.Dumper.ignore_aliases = lambda *args : True

PRINCIPAL_TYPE_GROUP = 'GROUP'
PRINCIPAL_TYPE_USER = 'USER'

TARGET_TYPE_ACCOUNT = 'AWS_ACCOUNT'

MAX_RESOURCES_PER_TEMPLATE = 200

REF_PREFIX = '!Ref='

Input = collections.namedtuple('Input', ['groups', 'users', 'permission_sets', 'ous', 'accounts'])

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('--profile', help="AWS profile to use to retrieve SSO instance and/or accounts from OUs")

    parser.add_argument('--instance', '-i', help="If not provided, will be retrieved from your account")

    principal_group = parser.add_argument_group("Principals")
    principal_group.add_argument('--groups', '-g', nargs='+', default=[])
    principal_group.add_argument('--users', '-u', nargs='+', default=[])

    parser.add_argument('--permission-sets', '-p', nargs='+', default=[])

    target_group = parser.add_argument_group("Targets")
    target_group.add_argument('--ous', '-o', nargs='+', default=[])
    target_group.add_argument('--accounts', '-a', nargs='+', default=[])

    parser.add_argument('--input-file', type=argparse.FileType('r'), help="INI file with section headers for principals, permission sets, and targets named the same as parameters.")

    parser.add_argument('--template-file', help="Template file name, defaults to template.yaml. When multiple templates are needed, numbers will be inserted.")

    parser.add_argument('--max-resources-per-template', type=int, default=MAX_RESOURCES_PER_TEMPLATE)

    args = parser.parse_args()

    if args.input_file:
        if args.groups or args.users or args.permission_sets or args.ous or args.accounts:
            parser.error("--input-file cannot be used with other parameters")
        else:
            instance, input = load_file(args.input_file, parser)
            if instance and not args.instance:
                args.instance = instance
            if instance and args.instance and instance != args.instance:
                parser.error("Instance {} from file and {} from command line do not match".format(instance, args.instance))
    else:
        input = Input(args.groups, args.users, args.permission_sets, args.ous, args.accounts)

    if not input.groups and not input.users:
        parser.error("Provide at least one principal (group or user)")

    if not input.permission_sets:
        parser.error("Provide at least one permission set")

    if not input.ous and not input.accounts:
        parser.error("Provide at least one target (OU or account)")

    session = [None]
    def get_session():
        if not session[0]:
            session[0] = boto3.Session(profile_name=args.profile)
        return session[0]

    try:
        if not args.instance:
            response = get_session().client('sso-admin').list_instances()
            if len(response['Instances']) == 0:
                parser.error("No SSO instance found, please specify with --instance")
            elif len(response['Instances']) > 1:
                parser.error("{} SSO instances found, please specify with --instance".format(len(response['Instances'])))
            else:
                instance_arn = response['Instances'][0]['InstanceArn']
                instance_id = instance_arn.split('/')[-1]
                print("Using SSO instance {}".format(instance_id))
                args.instance = instance_arn

        if input.ous:
            organizations_client = get_session().client('organizations')
            ou_fetcher = lambda ou: get_accounts_for_ou(organizations_client, ou)
        else:
            ou_fetcher = lambda ou: []

        templates = get_templates(args.instance, input, ou_fetcher, args.max_resources_per_template)
        if len(templates) == 1:
            if not args.template_file:
                args.template_file = 'template.yaml'
                print("Outputting to {}".format(args.template_file))
            template_file_names = [args.template_file]
        else:
            if not args.template_file:
                args.template_file = 'template.yaml'
            prefix, suffix = args.template_file.rsplit('.', 1)
            template_file_names = ["{}{:02d}.{}".format(prefix, num+1, suffix) for num in range(len(templates))]
            print("Outputting to {} through {}".format(template_file_names[0], template_file_names[-1]))

        for template_file_name, template in zip(template_file_names, templates):
            with open(template_file_name, 'w') as fp:
                yaml.dump(template, fp)

    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

def load_file(fp, parser):
    config = configparser.ConfigParser(allow_no_value=True, delimiters=['='])
    config.read_file(fp)

    def get_section(section):
        values = []
        try:
            for k, v in config.items(section):
                if k+'=' == REF_PREFIX.lower():
                    values.append('{}{}'.format(REF_PREFIX, v))
                else:
                    values.append(k)
            return values
        except configparser.NoSectionError:
            return []

    instance = get_section('instance')
    if len(instance) == 0:
        instance = None
    elif len(instance) == 1:
        instance = instance[0]
    else:
        parser.error("Multiple instances specified in file")

    groups = get_section('groups')
    users = get_section('users')

    permission_sets = get_section('permission-sets')

    ous = get_section('ous')
    accounts = get_section('accounts')

    return instance, Input(groups, users, permission_sets, ous, accounts)

def get_resource(instance_arn, principal, permission_set_arn, target):
    return collections.OrderedDict({
        "Type" : "AWS::SSO::Assignment",
        "Properties" : collections.OrderedDict({
                "InstanceArn" : instance_arn,
                "PrincipalType" : principal[0],
                "PrincipalId" : principal[1],
                "PermissionSetArn" : permission_set_arn,
                "TargetType" : target[0],
                "TargetId" : target[1],
            })
        })

def chunk_list_generator(lst, chunk_length):
    for i in range(0, len(lst), chunk_length):
        yield lst[i:i + chunk_length]

def get_templates(instance, input, ou_fetcher, max_resources_per_template):
    if instance.startswith('arn'):
        instance_arn = instance
    else:
        instance_arn = 'arn:aws:sso:::instance/{}'.format(instance)
    instance_id = instance_arn.split('/')[-1]

    principals = [(PRINCIPAL_TYPE_GROUP, g) for g in input.groups] + [(PRINCIPAL_TYPE_USER, u) for u in input.users]

    targets = []
    for ou in input.ous:
        targets.extend((TARGET_TYPE_ACCOUNT, account) for account in ou_fetcher(ou))
    targets.extend((TARGET_TYPE_ACCOUNT, account) for account in input.accounts)

    resources = []

    index = 1
    for principal in principals:
        if principal[1].startswith(REF_PREFIX):
            principal = (principal[0], {"Ref": principal[1][len(REF_PREFIX):]})
        for permission_set in input.permission_sets:
            if permission_set.startswith('arn'):
                permission_set_arn = permission_set
            elif permission_set.startswith(REF_PREFIX):
                permission_set_arn = {"Ref": permission_set[len(REF_PREFIX):]}
            elif permission_set.startswith('ssoins') or permission_set.startswith('ins'):
                permission_set_arn = 'arn:aws:sso:::permissionSet/{}'.format(permission_set)
            else:
                permission_set_arn = 'arn:aws:sso:::permissionSet/{}/{}'.format(instance_id, permission_set)
            for target in targets:
                if target[1].startswith(REF_PREFIX):
                    target = (target[0], {"Ref": target[1][len(REF_PREFIX):]})
                resource_name = 'Assignment{:03d}'.format(index)

                resources.append((resource_name, get_resource(instance_arn, principal, permission_set_arn, target)))

                index += 1

    templates = [collections.OrderedDict({
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": collections.OrderedDict(rsc),
    }) for rsc in chunk_list_generator(resources, max_resources_per_template)]

    return templates

def get_accounts_for_ou(organizations_client, ou):
    accounts = []

    paginator = organizations_client.get_paginator('list_organizational_units_for_parent')
    for response in paginator.paginate(ParentId=ou):
        sub_ous = [data['Id'] for data in response['OrganizationalUnits']]
        for sub_ou_id in sub_ous:
            accounts.extend(get_accounts_for_ou(organizations_client, sub_ou_id))

    paginator = organizations_client.get_paginator('list_accounts_for_parent')
    for response in paginator.paginate(ParentId=ou):
        accounts.extend(data['Id'] for data in response['Accounts'])

    return accounts

if __name__ == '__main__':
    main()
