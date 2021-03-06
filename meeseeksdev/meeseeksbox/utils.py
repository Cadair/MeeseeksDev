"""
Utility functions to work with github.
"""
import jwt
import datetime
import json
import requests
import re

from .scopes import Permission

API_COLLABORATORS_TEMPLATE = 'https://api.github.com/repos/{org}/{repo}/collaborators/{username}/permission'
ACCEPT_HEADER = 'application/vnd.github.machine-man-preview'
ACCEPT_HEADER_KORA = 'json,application/vnd.github.korra-preview'

"""
Regular expression to relink issues/pr comments correctly.

Pay attention to not relink things like foo#23 as they already point to a
specific repository.
"""
RELINK_RE = re.compile('(?:(?<=[:,\s])|(?<=^))(#\d+)\\b')



def fix_issue_body(body, original_poster, original_repo, original_org, original_number, migration_requester):
    """
    This, for now does only simple fixes, like link to the original issue.
    
    This should be improved to quote mention of people
    """

    body = RELINK_RE.sub('{org}/{repo}\\1'.format(org=original_org, repo=original_repo), body)
    
    return body + \
    """\n\n---- 
    \nOriginally opened as {org}/{repo}#{number} by @{reporter}, migration requested by @{requester}
    """.format(org=original_org, repo=original_repo, number=original_number, reporter=original_poster,  requester=migration_requester)

def fix_comment_body(body, original_poster, original_url, original_org, original_repo):
    """
    This, for now does only simple fixes, like link to the original comment.
    
    This should be improved to quote mention of people
    """

    body = RELINK_RE.sub('{org}/{repo}\\1'.format(org=original_org, repo=original_repo), body)
    
    return """[`@{op}` commented]({original_url}): {body}""".format(op=original_poster, original_url=original_url, body=body)

class Authenticator:
    
    def __init__(self, integration_id, rsadata, personnal_account_token, personnal_account_name):
        self.since = int(datetime.datetime.now().timestamp())
        self.duration = 60*10
        self._token = None
        self.integration_id = integration_id
        self.rsadata = rsadata
        self.personnal_account_token = personnal_account_token
        self.personnal_account_name = personnal_account_name
        # TODO: this mapping is built at startup, we should update it when we
        # have new / deleted installations
        self.idmap = {}
        self._session_class = Session

    def session(self, installation_id):
        return self._session_class(self.integration_id, self.rsadata, installation_id, 
                                   self.personnal_account_token,
                                   self.personnal_account_name,
                                   )
        
    def list_installations(self):
        """
        Todo: Pagination
        """
        response = self._integration_authenticated_request(
            'GET', "https://api.github.com/integration/installations")
        return response.json()

    def _build_auth_id_mapping(self):
        """
        Build an organisation/repo -> installation_id mappingg in order to be able
        to do cross repository operations.
        """

        installations = self.list_installations()
        for installation in installations:
            iid = installation['id']
            session = self.session(iid)
            repositories = session.ghrequest(
                'GET', installation['repositories_url'], json=None).json()
            for repo in repositories['repositories']:
                self.idmap[repo['full_name']] = iid



    def _integration_authenticated_request(self, method, url):
        self.since= int(datetime.datetime.now().timestamp())
        payload = dict({
          'iat': self.since,
          'exp': self.since + self.duration,
          'iss': self.integration_id,
        })

        tok = jwt.encode(payload, key=self.rsadata, algorithm='RS256')

        headers = {'Authorization': 'Bearer {}'.format(tok.decode()),
                   'Accept': ACCEPT_HEADER,
                   'Host': 'api.github.com',
                   'User-Agent': 'python/requests'}
        req = requests.Request(method, url, headers=headers)
        prepared = req.prepare()
        with requests.Session() as s:
            return s.send(prepared)
        

class Session(Authenticator):

    def __init__(self, integration_id, rsadata, installation_id, personnal_account_token, personnal_account_name):
        super().__init__(integration_id, rsadata, personnal_account_token, personnal_account_name)
        self.installation_id = installation_id

    def token(self):
        now = datetime.datetime.now().timestamp()
        if (now > self.since + self.duration-60) or (self._token is None):
            self.regen_token()
        return self._token
        
    def regen_token(self):
        method = 'POST'
        url = 'https://api.github.com/installations/%s/access_tokens'%self.installation_id
        resp = self._integration_authenticated_request(method, url)
        try:
            self._token = json.loads(resp.content.decode())['token']
        except:
            raise ValueError(resp.content, url)

    def personal_request(self, method, url, json=None):
        """
        Does a request but using the personal account name and token
        """
        if not json:
            json = {}
        def prepare():
            headers = {'Authorization': 'token {}'.format(self.personnal_account_token),
                       'Host': 'api.github.com',
                       'User-Agent': 'python/requests'}
            req = requests.Request(method, url, headers=headers, json=json)
            return req.prepare()

        with requests.Session() as s:
            response = s.send(prepare())
            if response.status_code == 401:
                self.regen_token()
                response = s.send(prepare())
            response.raise_for_status()
            return response


    def ghrequest(self, method, url, json=None, *, override_accept_header=None):
        accept = ACCEPT_HEADER
        if override_accept_header:
            accept = override_accept_header
        def prepare():
            atk = self.token()
            headers = {'Authorization': 'Bearer {}'.format(atk),
                       'Accept': accept,
                       'Host': 'api.github.com',
                       'User-Agent': 'python/requests'}
            req = requests.Request(method, url, headers=headers, json=json)
            return req.prepare()

        with requests.Session() as s:
            response = s.send(prepare())
            if response.status_code == 401:
                self.regen_token()
                response = s.send(prepare())
            response.raise_for_status()
            return response

    def _get_permission(self, org, repo, username):
        get_collaborators_query = API_COLLABORATORS_TEMPLATE.format(
            org=org, repo=repo, username=username)
        resp = self.ghrequest('GET', get_collaborators_query, None, override_accept_header=ACCEPT_HEADER_KORA)
        resp.raise_for_status()
        permission = resp.json()['permission']
        print("found permission", permission  , "for user ", username, "on ", org, repo)
        return getattr(Permission, permission)

    def has_permission(self, org, repo, username, level=None):
        """
        """
        if not level:
            level = Permission.none

        return self._get_permission(org, repo, username).value >= level.value

    def post_comment(self, comment_url, body):
        self.ghrequest('POST', comment_url, json={"body":body})

    def get_collaborator_list(self, org, repo):
        get_collaborators_query = 'https://api.github.com/repos/{org}/{repo}/collaborators'.format(org=org, repo=repo)
        resp = self.ghrequest('GET', get_collaborators_query, None)
        if resp.status_code == 200:
            return resp.json()
        else:
            resp.raise_for_status()

    def create_issue(self, org:str, repo:str , title:str, body:str, *, labels=None, assignees=None):
        arguments = {
            "title": title,
            "body": body,
        }
        
        if labels:
            if type(labels) in (list, tuple):
                arguments['labels'] = labels
            else:
                raise ValueError('Labels must be a list of a tuple')
            
        if assignees:
            if type(assignees) in (list, tuple):
                arguments['assignees'] = assignees
            else:
                raise ValueError('Assignees must be a list or a tuple')
            
        return self.ghrequest('POST', 'https://api.github.com/repos/{}/{}/issues'.format(org, repo),
                    json=arguments)
