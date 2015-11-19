import os
import requests
import json
import time
import sys
import csv
from minion.plugins.base import BlockingPlugin


class NessusPlugin(BlockingPlugin):
    PLUGIN_NAME = 'Nessus'
    PLUGIN_VERSION = '0.1'
    # previously set to 'light', nessus requires a significant amount of
    # network resources
    PLUGIN_WEIGHT = 'medium'

    server_url = ''
    _verify = True
    _token = ''
    _username = ''
    _password = ''

    def do_run(self):
        self.logger.debug('do_run')
        self.login()

        policies = self.get_policies()
        policy_name = self.configuration.get('policy')
        target_name = self.configuration.get('target')
        scan_name = self.configuration.get('scan_name')
        scan_description = self.configuration.get('scan_description')

        policy_id = policies[policy_name]
        scan_data = self.add(scan_name, scan_description,
                             target_name, policy_id)
        scan_id = scan_data['id']

        scan_uuid = self.launch(scan_id)
        history_ids = self.get_history_ids(scan_id)
        history_id = history_ids[scan_uuid]
        while self.status(scan_id, history_id) != 'completed':
            time.sleep(5)

        file_id = self.export(scan_id, history_id)
        data = self.download(scan_id, file_id)
        self.parse_csv_data(data)

    def do_configure(self, configuration=None, enable_logging=True,
                     logging_file='/var/log/minion/nessus.log'):
        """
        Initialize the nmap plugin (aka __init__)

        This sets up some internal variables:
            SEVERITY_ORDER: list of severities from info to critical
            SEVERITY_MAPPING: severity level (ie, medium) to its numerical
              position (ie, 2) for severity comparisons
            DEFAULT_SEVERITY: a default severity to raise for issues if there
              is neither a port nor version severity

        Args:
            configuration:  allows the forcible setting of configuration (the
                'configuration' section of a plugin's plan
                when not being run through minion directly; very useful for
                test cases
            enable_logging: if set to True, allows a plugin writer to create
                debug logs (ala console.log) with self.logger.debug(msg)
            logging_file:   where to store the debug log generated by setting
                enable_logging to True
        """

        self.server_url = self.configuration.get('server_url')
        self._username = os.environ.get('NESSUS_USER', '')
        self._password = os.environ.get('NESSUS_PASS', '')
        self._verify = self.configuration.get('verify')

        # This is useful for testing, where a new instance can pass in a
        # configuration object
        if configuration:
            self.configuration = configuration

        # Variables to hold stdout and stderr
        self.stdout = self.stderr = ''

        # Enable logging during development
        if enable_logging:
            import logging
            self.logger = logging.getLogger('minion-plugin-nessus')
            self.logger.setLevel(logging.DEBUG)

            fh = logging.FileHandler(logging_file)
            self.logger.addHandler(fh)
        self.logger.debug('do_configure')

    def build_url(self, resource):
        return '{0}{1}'.format(self.server_url, resource)

    def connect(self, method, resource, data=None):
        """
        Send a request

        Send a request to Nessus based on the specified data. If the session
        token is available add it to the request. Specify the content type as
        JSON and convert the data to JSON format.
        """
        self.logger.debug('connect')
        headers = {'X-Cookie': 'token={0}'.format(self._token),
                   'content-type': 'application/json'}

        data = json.dumps(data)

        if method == 'POST':
            r = requests.post(self.build_url(resource),
                              data=data, headers=headers, verify=self._verify)
        elif method == 'PUT':
            r = requests.put(self.build_url(resource),
                             data=data, headers=headers, verify=self._verify)
        elif method == 'DELETE':
            r = requests.delete(self.build_url(resource),
                                data=data, headers=headers,
                                verify=self._verify)
        else:
            r = requests.get(self.build_url(resource),
                             params=data, headers=headers, verify=self._verify)

        if r.status_code != 200:
            e = r.json()
            self.logger.error('Unexpected response from Nessus' + e['error'])

        # When downloading a scan we need the raw contents not the JSON data.
        if 'download' in resource or method == 'DELETE':
            return r.content
        else:
            return r.json()

    def login(self):
        """
        Login to nessus.
        """
        self.logger.debug('login')
        login = {'username': self._username, 'password': self._password}
        data = self.connect('POST', '/session', data=login)

        self._token = data['token']
        return data['token']

    def logout(self):
        """
        Logout of nessus.
        """
        self.logger.debug('logout')
        self.connect('DELETE', '/session')

    def get_policies(self):
        """
        Get scan policies

        Get all of the scan policies but return only the title and the uuid of
        each policy.
        """
        self.logger.debug('get_policies')

        data = self.connect('GET', '/editor/policy/templates')

        return dict((p['title'], p['uuid']) for p in data['templates'])

    def get_history_ids(self, sid):
        """
        Get history ids

        Create a dictionary of scan uuids and history ids so we can lookup the
        history id by uuid.
        """
        self.logger.debug('get_history_ids')
        data = self.connect('GET', '/scans/{0}'.format(sid))

        return dict((h['uuid'], h['history_id']) for h in data['history'])

    def get_scan_history(self, sid, hid):
        """
        Scan history details

        Get the details of a particular run of a scan.
        """
        self.logger.debug('get_scan_history')
        params = {'history_id': hid}
        data = self.connect('GET', '/scans/{0}'.format(sid), params)

        return data['info']

    def add(self, name, desc, targets, pid):
        """
        Add a new scan

        Create a new scan using the policy_id, name, description and targets.
        The scan will be created in the default folder for the user. Return
        the id of the newly created scan.
        """
        self.logger.debug('add')

        scan = {'uuid': pid,
                'settings': {
                    'name': name,
                    'description': desc,
                    'text_targets': targets}
                }

        data = self.connect('POST', '/scans', data=scan)

        return data['scan']

    def update(self, scan_id, name, desc, targets, pid=None):
        """
        Update a scan

        Update the name, description, targets, or policy of the specified scan.
        If the name and description are not set, then the policy name and
        description will be set to None after the update. In addition the
        targets value must be set or you will get an
        "Invalid 'targets' field" error.
        """
        self.logger.debug('update')

        scan = {}
        scan['settings'] = {}
        scan['settings']['name'] = name
        scan['settings']['desc'] = desc
        scan['settings']['text_targets'] = targets

        if pid is not None:
            scan['uuid'] = pid

        data = self.connect('PUT', '/scans/{0}'.format(scan_id), data=scan)

        return data

    def launch(self, sid):
        """
        Launch a scan

        Launch the scan specified by the sid.
        """
        self.logger.debug('launch')
        data = self.connect('POST', '/scans/{0}/launch'.format(sid))

        return data['scan_uuid']

    def status(self, sid, hid):
        """
        Check the status of a scan run

        Get the historical information for the particular scan and hid. Return
        the status if available. If not return unknown.
        """
        self.logger.debug('status')

        d = self.get_scan_history(sid, hid)
        return d['status']

    def export_status(self, sid, fid):
        """
        Check export status

        Check to see if the export is ready for download.
        """
        self.logger.debug('export_status')

        data = self.connect('GET',
                            '/scans/{0}/export/{1}/status'.format(sid, fid))

        return data['status'] == 'ready'

    def export(self, sid, hid, data_format='csv'):
        """
        Make an export request

        Request an export of the scan results for the specified scan and
        historical run. In this case the format is hard coded as nessus but
        the format can be any one of nessus, html, pdf, csv, or db. Once the
        request is made, we have to wait for the export to be ready.
        """
        self.logger.debug('export')

        data = {'history_id': hid,
                'format': data_format}

        data = self.connect('POST', '/scans/{0}/export'.format(sid), data=data)

        fid = data['file']

        while self.export_status(sid, fid) is False:
            time.sleep(5)

        return fid

    def download(self, sid, fid):
        """
        Download the scan results

        Download the scan results stored in the export file specified by fid
        for the scan specified by sid.
        """
        self.logger.debug('download')

        data = self.connect('GET',
                            '/scans/{0}/export/{1}/download'.format(sid, fid))
        return data

    def minion_severity(self, risk):
        self.logger.debug('minion_severity')
        if risk == 'None':
            return 'Info'
        else:
            return risk

    def _get_plugin_name(self, plugin_info):
        self.logger.debug('_get_plugin_name')
        name = None
        for attribute in plugin_info['attributes']:
            if attribute['attribute_name'] == 'plugin_name':
                name = attribute['attribute_value']
        return name

    def _build_description(self, row, plugin_name):
        self.logger.debug('_build_description')
        return plugin_name + ' ' + row[8] + ' ' + row[9] + ' ' + \
            row[10] + ' ' + row[12]

    def create_issue(self, row, plugin_info):
        """
        0  Plugin ID
        1  CVE
        2  CVSS
        3  Risk
        4  Host
        5  Protocol
        6  Port
        7  Name
        8  Synopsis
        9  Description
        10 Solution
        11 See Also
        12 Plugin Output
        """
        self.logger.debug('create_issue')
        plugin_name = self._get_plugin_name(plugin_info)
        val = {
            "Severity": self.minion_severity(row[3]),
            "Summary": row[8],
            "Description": self._build_description(row, plugin_name),
            "URLs": [{"URL": "{h}:{p}".format(h=row[4], p=row[6])}],
            "Ports": [row[6]],
        }
        return val

    def parse_csv_data(self, data):
        """
        Parses CSV data into a data structure for Minion
        From
            Plugin ID, CVE, CVSS, Risk, Host, Protocol, Port, Name, Synopsis,
              Description Solution, See Also, Plugin Output
        To
        {
            'Severity': 'High',
            'Summary': '10.0.1.1: open port (88), running: Heimdal Kerberos' \
                       ' (unrecognized software)',
            'Description': '10.0.1.1: open port (88), running: Heimdal' \
                           ' Kerberos (unrecognized software)',
            'URLs': [{'URL': '10.0.1.1:88'}],
            'Ports': [88],
            'Classification': {
                'cwe_id': '200',
                'cwe_url': 'http://cwe.mitre.org/data/definitions/200.html'
            }
        }
        """
        self.logger.debug('parse_csv_data')
        plugins = dict()
        rows = data.splitlines()
        issues = []
        for row in csv.reader(rows):
            if row[0] == 'Plugin ID':
                continue
            if row[0] not in plugins:
                plugins[row[0]] = self.get_plugin_info(row[0])
            self.report_issue(self.create_issue(row, plugins[row[0]]))
        self.report_finish()
        return issues

    def delete(self, sid):
        """
        Delete a scan

        This deletes a scan and all of its associated history. The scan is
        not moved to the trash folder, it is deleted.
        """
        self.logger.debug('delete')

        self.connect('DELETE', '/scans/{0}'.format(sid))

    def history_delete(self, sid, hid):
        """
        Delete a historical scan.

        This deletes a particular run of the scan and not the scan itself.
        The scan run is defined by the history id.
        """
        self.logger.debug('history_delete')

        self.connect('DELETE', '/scans/{0}/history/{1}'.format(sid, hid))

    def get_plugin_info(self, pid):
        self.logger.debug('get_plugin_info')

        return self.connect('GET', '/plugins/plugin/{0}'.format(pid))
