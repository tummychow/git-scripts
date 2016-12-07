import subprocess
import urllib.parse
import urllib.request
import json
import codecs
UTF8Reader = codecs.getreader('utf-8')


def deprefix(val, prefix, check=False):
    if val.startswith(prefix):
        return val[len(prefix):]
    elif check:
        raise RuntimeError('{!r} does not start with {!r}'.format(val, prefix))
    return val


def desuffix(val, suffix, check=False):
    if val.endswith(suffix):
        return val[:-len(suffix)]
    elif check:
        raise RuntimeError('{!r} does not end with {!r}'.format(val, suffix))
    return val


def invoke(*cmd):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, universal_newlines=True).stdout


def git_config_get(*names, default=None, get_all=False):
    action = '--get'
    if get_all:
        action = '--get-all'
        if default is None:
            default = []

    for variable in names:
        try:
            ret = invoke('git', 'config', action, '--null', variable).split('\0')[:-1]
            if not get_all:
                [ret] = ret
            return ret
        except subprocess.CalledProcessError as e:
            if e.returncode != 1:
                raise
    if default is None:
        raise RuntimeError('git config did not contain any of {!r}'.format(names))
    return default


def github_from_remote_url(remote_url):
    # the format of these urls is defined in git-fetch
    # it can be either an https url, an ssh url, or an scp-style string
    [before_colon, after_colon] = remote_url.split(':', maxsplit=1)
    if after_colon.startswith('//'):
        # url case
        parsed_url = urllib.parse.urlparse(remote_url)
        # TODO: the user could have other hosts here:
        #       - a resolved ip
        #       - a proxy host
        #       - a cname or alias from their /etc/hosts
        #       - a host from their ssh config
        if parsed_url.hostname != 'github.com':
            raise RuntimeError('{!r} does not point to github.com'.format(remote_url))
        # TODO: the user might have a different username here, or no username
        # at all (eg from their ssh config)
        if parsed_url.scheme != 'https' and (parsed_url.scheme != 'ssh' or parsed_url.user != 'git'):
            raise RuntimeError('{!r} does not use https, or ssh as git'.format(remote_url))
        # TODO: the user could specify a port explicitly
        if not parsed_url.path.startswith('/'):
            raise RuntimeError('{!r} path was parsed incorrectly to {}'.format(remote_url, parsed_url.path))
        owner_slash_repo = parsed_url.path[1:]
    else:
        # scp case
        # TODO: similar to above, this might not be the same user or hostname
        if before_colon != 'git@github.com':
            raise RuntimeError('{!r} uses scp style syntax, but does not point to git@github.com'.format(remote_url))
        owner_slash_repo = after_colon

    [owner, repo] = desuffix(owner_slash_repo, '.git').split('/')
    return owner, repo


def https_get_json(host, path='', params={}, headers={}):
    with urllib.request.urlopen(urllib.request.Request(
        urllib.parse.urlunparse(('https', host, path, '', urllib.parse.urlencode(params), '')),
        method='GET',
        headers=headers,
    )) as resp:
        return json.load(UTF8Reader(resp))


def get_remote_or_github_default(remote_name, headers, owner_repo=None, default=None):
    try:
        # try to resolve the remote's HEAD first
        # see also git-remote set-head
        remote_head = invoke('git', 'symbolic-ref', '--short', 'refs/remotes/{}/HEAD'.format(remote_name)).strip()
        [found_remote, default_branch] = remote_head.split('/', maxsplit=1)
        if found_remote != remote_name:
            # TODO: is it ever possible for a remote's HEAD to not point to a
            # ref of that remote itself? is this check necessary?
            raise RuntimeError('remote {} has default branch {}, which does not belong to that remote'.format(remote_name, default_branch))
        return default_branch
    except subprocess.CalledProcessError as e:
        if default is not None:
            return default
        # we could compute this ourselves, but we don't know if the caller
        # wants the fetch or push url
        remote_owner, remote_repo = owner_repo
        repo_data = https_get_json(
            'api.github.com',
            '/repos/{}/{}'.format(remote_owner, remote_repo),
            headers=headers,
        )
        return repo_data['default_branch']
