# problem statement

we are in a git repository. we want to invoke `git gpr` in the repo, which tells our browser to open a page that will create a pull request for the branch we are currently on. this statement comes with many assumptions:

- we are currently on a branch (what if we aren't?)
- our current branch has been pushed to a github remote
- the branch we want to merge into is also on a github remote
- we know what those remote branches are

let's consider the problem from the output first. we want to get a url of the form:

```
https://github.com/<targetowner>/<targetrepo>/compare/<targetbranch>...[<candidateowner>:]<candidatebranch>?expand=1
```

we have highlighted five important parameters in this url:

- the **candidate branch**, which we want to be merged.
- the **target branch**, which we want to merge the candidate branch into.
- the **target owner** and **target repo**, which specify the repository containing the target branch.
- the **candidate owner**, specifying whose fork of the target repo contains the candidate branch. this parameter can be omitted if both branches are in the same repo.

to compute these 4-5 parameters, what information do we have?

- the git repository we're currently in. however for the sake of compatibility, we are forbidden from reading any files in `.git`, since their exact contents could change in future git versions.
- the github api, which we can use to get more information about the repositories once we know what they are. but this should be used sparingly since it's rate-limited.

that's all we're allowed to use. we are not allowed to ask the user for any input or command line arguments. our implementation will use git 2.10.0 and python 3.5.2 since those are the things my computer has.

# step 1: the local branch

the candidate branch is something that only exists in github. before we can find it, we first have to find the branch we're on, and then relate that to the candidate branch somehow. we refer to the branch we're currently on as the **local branch**.

there is a canonical way to find this: [`git-symbolic-ref`](https://git-scm.com/docs/git-symbolic-ref). if you're on a branch, then HEAD will be a symbolic ref to that branch's own ref. i have never seen a case where HEAD was a symbolic ref to something that wasn't a branch - either it's a symbolic ref to a branch, or a direct ref to a commit. (this is true if you check out a tag or a remote ref directly as well.)

since `gpr` assumes we're on a branch, we can bail out if `symbolic-ref` fails, so we can retrieve HEAD simply like so:

```python
# NOTE: we will be using this again later
import subprocess
def invoke(*cmd):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, universal_newlines=True).stdout

HEAD = invoke('git', 'symbolic-ref', '--short', 'HEAD').strip()
```

the `--short` flag is acceptable since we know that it must be a branch, and the `refs/heads/` prefix would get in our way. (we could leave out `--short` and check for the `refs/heads/` prefix, if we wanted to be absolutely sure that this was in fact a branch.)if we wanted to suppress output on failure, we could add `-q`, but for our purposes, showing the error output is probably desirable.

# step 2: the candidate owner and repo

now that we know the local branch's identity, how do we determine where it is on the internet? the heuristic `gpr` will use is that the candidate is whatever you get, when you [`git-push`](https://git-scm.com/docs/git-push) without arguments.

## step 2a: the candidate remote

unfortunately, figuring this out is not nearly as simple as it sounds. the behavior is fairly complex and has evolved over the lifespan of git. fortunately, the behavior is well-documented in respect to how complex it is. the candidate remote can be determined by one of three variables, from most to least precedence:

- `branch.<name>.pushRemote`, which specifies the remote to push to on a particular branch (ignored if you aren't on a branch)
- `remote.pushDefault`, which specifies the default remote to push to, no matter what you have checked out
- `branch.<name>.remote`, which specifies the remote to pull from on a particular branch (ignored if you aren't on a branch)

if none of these are set, `push` without arguments will fail. (they could end up resolving to the local repo, `.`, but that will fail later, so we don't need to special-case it here.) we can compute this by invoking a monadic chain of [`git-config`](https://git-scm.com/docs/git-config) commands:

```python
# NOTE: we will be using this again later
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

CANDIDATE_REMOTE = git_config_get(
    'branch.{}.pushRemote'.format(HEAD),
    'remote.pushDefault',
    # TODO: does the default of 'origin' apply here, or does git-push fail if
    # they are all unset?
    'branch.{}.remote'.format(HEAD),
)
```

you might notice that we are using `--get-all --null`, which safely handles config variables with multiple values (even if those values include newlines). the variables we're concerned with here should only be one value each, but we'll be working with other variables later that can have multiple values, so we need to handle those sanely.

## step 2b: the candidate remote's url

once we have the name of the candidate remote, we have to resolve that to a url. a little-known feature of git remotes is that they can have multiple urls, one for fetching and one for pushing. we specifically want to retrieve the push url. we could do this via another `config` command, but there's a safer way using [`git-remote`](https://git-scm.com/docs/git-remote)'s `get-url` command, which does the defaulting behavior for us:

```python
CANDIDATE_URL = invoke('git', 'remote', 'get-url', '--push', CANDIDATE_REMOTE).strip()
```

## step 2c: splitting the url into owner and repo

now that we have the url, we have to find out if it belongs to github at all. github only supports remotes over https or ssh, resulting in one of three possible forms (taking into account the specs on [`git-fetch`](https://git-scm.com/docs/git-fetch), and github's own constraints):

- `https://github.com/<owner>/<repo>[.git]`
- `ssh://git@github.com/<owner>/<repo>[.git]`
- `git@github.com:<owner>/<repo>[.git]`

some things that all these formats have in common: they all include the github host with an unqualified port, and they all have an optional `.git` suffix. two of them are actually urls and can be parsed as such, but the third form is scp-style and would require manual decomposition. the host could actually be very strangely shaped:

- it might be resolved down to an ip instead of just `github.com`
- it might be a hostname inside a corporate firewall that redirects to `github.com`
- it might include an explicit port (443 or 22)
- it might refer to stuff in the user's ssh config (eg the user defines a host `github` that maps to `github.com`, or they set `github.com` to always use `git` as its user)

we're going to ignore all these for now and assume the happy case where it really is just `github.com`. we need to validate that the url is in fact for github, and then parse out the owner/repo segments:

```python
# NOTE: we will be using this again later
def desuffix(val, suffix, check=False):
    if val.endswith(suffix):
        return val[:-len(suffix)]
    elif check:
        raise RuntimeError('{!r} does not end with {!r}'.format(val, suffix))
    return val
import urllib.parse
def github_from_remote_url(remote_url):
    [before_colon, after_colon] = remote_url.split(':', maxsplit=1)
    if after_colon.startswith('//'):
        parsed_url = urllib.parse.urlparse(remote_url)
        if parsed_url.hostname != 'github.com':
            raise RuntimeError('{!r} does not point to github.com'.format(remote_url))
        if parsed_url.scheme != 'https' and (parsed_url.scheme != 'ssh' or parsed_url.user != 'git'):
            raise RuntimeError('{!r} does not use https, or ssh as git'.format(remote_url))
        if not parsed_url.path.startswith('/'):
            raise RuntimeError('{!r} path was parsed incorrectly to {}'.format(remote_url, parsed_url.path))
        owner_slash_repo = parsed_url.path[1:]
    else:
        if before_colon != 'git@github.com':
            raise RuntimeError('{!r} uses scp style syntax, but does not point to git@github.com'.format(remote_url))
        owner_slash_repo = after_colon

    [owner, repo] = desuffix(owner_slash_repo, '.git').split('/')
    return owner, repo

CANDIDATE_OWNER, CANDIDATE_REPO = github_from_remote_url(CANDIDATE_URL)
```

the conditional parts of this code are fairly nasty, but at last we have the candidate owner and repository.

# step 3: the candidate branch

going back to what we said earlier, the candidate is whatever you get when you `push` with no arguments. we've resolved the remote for that case, now what about the branch? this behavior is again governed by two config variables.

## step 3a: `remote.<name>.push`

this config variable consists of a list of refspecs. if the list is nonempty, then we have to check the source side of each refspec to see if it matches the local branch name. if it does, then the corresponding destination side of the refspec is our candidate branch. if none of them match, then we bail out.

this "match" behavior is a lot more complicated than it sounds, because refspecs have a lot of behavior built into them. the refspec source could omit the `refs/` prefix (and potentially be ambiguous if so). it could contain a single `*` (in which case it's a pattern, as briefly noted in [`git-check-ref-format`](https://git-scm.com/docs/git-check-ref-format)).

"match" can occur in two ways. the easy way is a literal match - if the local branch and the source ref are equal (taking into account a possible `refs/heads/` prefix), then that counts as a match.

the more complicated case is if the source ref is a pattern (as briefly noted in [`git-check-ref-format`](https://git-scm.com/docs/git-check-ref-format)). a "pattern", in this case, can contain exactly one `*`. the part before the `*` must be a prefix of the branch name, and the part after the `*` must be a suffix of the branch name. in that case, the pattern is said to match the branch name, and if there is another `*` in the destination, then it is replaced by the part of the branch name that is left over in the middle. (eg `foobarbaz` matches `foo*baz`, and if the destination had pattern `baz*foo`, it would be resolved to `bazbarfoo`.)

```python
# NOTE: we will be using this again later
def deprefix(val, prefix, check=False):
    if val.startswith(prefix):
        return val[len(prefix):]
    elif check:
        raise RuntimeError('{!r} does not start with {!r}'.format(val, prefix))
    return val
PULL_REMOTE = git_config_get('branch.{}.remote'.format(HEAD), default='origin')
PULL_BRANCHES = [deprefix(branch, 'refs/heads/', check=True) for branch in git_config_get('branch.{}.merge'.format(HEAD), get_all=True)]

push_specs = git_config_get('remote.{}.push'.format(CANDIDATE_REMOTE), get_all=True)
if len(push_specs) != 0:
    symbolic_head = 'refs/heads/' + HEAD
    for push_refspec in push_specs:
        # TODO: are there forms here that don't include colons? eg 'tag <tag>'?
        [src, dst] = push_refspec.split(':')
        src = deprefix(src, '+')
        if src == '' and dst == '':
            CANDIDATE_BRANCH = HEAD
            break
        elif '*' in src:
            [src_prefix, src_suffix] = src.split('*')
            # TODO: how does the 'refs/<>/' prefix get involved here? we try
            # with 'refs/heads/' and without it, is that sufficient?
            if HEAD.startswith(src_prefix) and HEAD.endswith(src_suffix):
                head_middle = HEAD[len(src_prefix):-len(src_suffix)]
            elif symbolic_head.startswith(src_prefix) and symbolic_head.endswith(src_suffix):
                head_middle = symbolic_head[len(src_prefix):-len(src_suffix)]
            else:
                continue
            [dst_prefix, dst_suffix] = dst.split('*')
            CANDIDATE_BRANCH = dst_prefix + head_middle + dst_suffix
            break
        else:
            # TODO: is the source ref allowed to be nonexistent? if so, should
            # we just continue when this fails?
            symbolic_src = invoke('git', 'rev-parse', '--symbolic-full-name', src).strip()
            if symbolic_head == symbolic_src:
                CANDIDATE_BRANCH = dst
                break
    else:
        # TODO: should we fall through to push.default here? does git do that?
        raise RuntimeError('remote.{}.push is defined, but none of {!r} match {}'.format(CANDIDATE_REMOTE, push_specs, HEAD))
    if CANDIDATE_BRANCH.startswith('refs/'):
        # TODO: besides 'refs/heads/<name>' and <name>, what other forms could
        # be valid here?
        # TODO: is 'refs/remotes/<name>/<foo>' allowed here? that actually
        # seems to push our branch, to one of the remote's remote refs
        # TODO: what if dst is actually a tag? (unlikely since tags cannot be
        # updated by branches without '+', but 'refs/tags/' has higher priority
        # than 'refs/heads/')
        CANDIDATE_BRANCH = deprefix(CANDIDATE_BRANCH, 'refs/heads/', check=True)
else:
    # continued below
```

for those following along at home, it was right around this point in the game that i contemplated using a libgit2 binding. (if you're curious, i would have probably used rugged because those are the bindings github uses in production, and therefore the most complete. however, [this functionality](https://libgit2.github.com/libgit2/#HEAD/group/refspec) is so esoteric that i'm not sure even rugged has bound to it yet.)

## step 3b: `push.default`

in the event that there are no push refspecs configured for the candidate remote, we must fall back to the behavior of `push.default`, a classic git config variable which selects the default branch to push to based on its value:

- `upstream`: the branch we push to is the same as the one we pull from, `branch.<name>.merge` (bail if there are multiple, because it would be ambiguous)
- `matching` or `current`: the branch we push to has the same name as the branch we're on
- `nothing`: the user would not be allowed to `push` with no arguments, so we'd probably have to bail here
- `simple` or unset: if `branch.<name>.remote` (defaulting to `origin`) matches the remote we are pushing to (ie the candidate remote), then behave like `upstream`, else like `current`

```python
else:
    # continued from above
    push_default = git_config_get('push.default', default='simple')
    if push_default == 'matching' or push_default == 'current' or (push_default == 'simple' and CANDIDATE_REMOTE != PULL_REMOTE):
        CANDIDATE_BRANCH = HEAD
    elif push_default == 'upstream' or (push_default == 'simple' and CANDIDATE_REMOTE == PULL_REMOTE):
        [CANDIDATE_BRANCH] = PULL_BRANCHES
    elif push_default == 'nothing':
        raise RuntimeError('push.default is nothing, cannot determine candidate')
    else:
        raise RuntimeError('push.default has unrecognized value {}'.format(push_default))
```

# step 4: the target owner, repo, and branch

determining the target repo is a harder task than determining the candidate repo. in many corporate environments, the two repos will be the same (aka "centralized workflow"). in open-source environments, the candidate will probably be a fork of the target, or possibly a multi-forked descendant. since the target repo and the target branch are closely related, we resolve them using the same heuristic:

- if [`git-pull`](https://git-scm.com/docs/git-pull) and `push` go to different places, then we will use `pull`'s source as the target. by "different places" we mean either in remote or branch - the user could have two completely different remotes (aka "decentralized workflow") or two different branches on the same remote. (if there are multiple pull branches, we'll use the pull remote's default branch, and if that doesn't exist either, we'll use the default branch on github.)
- if the candidate repo is not a fork of another repo, we will use it as the target remote, and either its git default branch, or its github default branch, as the target branch.
- if the candidate repo is a fork of another repo, we will use the fork as the target repo (we will not go up multiple forks). if we have a remote for that fork, we'll use that remote's default branch in git, otherwise we'll use the fork's github default.

## step 4a: common code

all three paths above have a similar heuristic of "get the remote's default, or the github default if we don't have one". let's implement the common code for that first. we don't want to repeat requests to github, so we need a default option if we already performed the request earlier.

```python
import urllib.request
import codecs
import json
UTF8Reader = codecs.getreader('utf-8')
def https_get_json(host, path='', params={}, headers={}):
    with urllib.request.urlopen(urllib.request.Request(
        urllib.parse.urlunparse(('https', host, path, '', urllib.parse.urlencode(params), '')),
        method='GET',
        headers=headers,
    )) as resp:
        return json.load(UTF8Reader(resp))

GITHUB_OAUTH_TOKEN = git_config_get('github.oauth')
GITHUB_HEADERS = {
    'Accept': 'application/vnd.github.v3+json',
    'Authorization': 'token {}'.format(GITHUB_OAUTH_TOKEN)
}

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
        remote_owner, remote_repo = owner_repo
        repo_data = https_get_json(
            'api.github.com',
            '/repos/{}/{}'.format(remote_owner, remote_repo),
            headers=headers,
        )
        return repo_data['default_branch']
```

## step 4b: `pull` defaults

when invoked without arguments, `fetch` uses `branch.<name>.remote` to determine the remote of choice. like before, this could also resolve to `.`, but we don't need any handling for that. most of this code is basically the same as before.

```python
if PULL_REMOTE != CANDIDATE_REMOTE or CANDIDATE_BRANCH not in PULL_BRANCHES:
    PULL_URL = invoke('git', 'remote', 'get-url', PULL_REMOTE).strip()
    TARGET_OWNER, TARGET_REPO = github_from_remote_url(PULL_URL)
    if len(PULL_BRANCHES) == 1:
        [TARGET_BRANCH] = PULL_BRANCHES
    else:
        TARGET_BRANCH = get_remote_or_github_default(PULL_REMOTE, headers=GITHUB_HEADERS, owner_repo=(TARGET_OWNER, TARGET_REPO))
else:
    # continued below
```

## step 4c: find fork parent

if we push and pull from the same remote (we'll deal with the branch later), then we need to find the target somewhere else. we can ask the github api if we're a fork of another repo, and if so, what that is.

```python
else:
    # continued from above
    repo_data = https_get_json(
        'api.github.com',
        '/repos/{}/{}'.format(CANDIDATE_OWNER, CANDIDATE_REPO),
        headers=GITHUB_HEADERS
    )
    if repo_data['fork']:
        repo_data = repo_data['parent']
        # TODO: find a matching remote for the fork, and try to get its default
        TARGET_BRANCH = get_remote_or_github_default('ENOENT', headers=GITHUB_HEADERS, default=repo_data['default_branch'])
    else:
        TARGET_BRANCH = get_remote_or_github_default(CANDIDATE_REMOTE, headers=GITHUB_HEADERS, default=repo_data['default_branch'])

    TARGET_OWNER = repo_data['owner']['login']
    TARGET_REPO = repo_data['name']
```

# step 5: opening the url

having computed the candidate owner/repo/branch and target owner/repo/branch, we can finally compute and open a url for the pull request in question. here we will add one convenience: if a pull request already exists for what we want to open, we will try to open that instead.

## step 5a: nop if equal

if the candidate and target are literally identical, then there's nothing for us to do.

```python
if TARGET_OWNER == CANDIDATE_OWNER and TARGET_REPO == CANDIDATE_REPO and TARGET_BRANCH == CANDIDATE_BRANCH:
    raise RuntimeError('target and candidate are identical ({}/{} {})'.format(TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
```

## step 5b: finding existing pull requests

we can make a query about the pulls against the target repo like so:

```python
pulls_body = https_get_json(
    'api.github.com',
    '/repos/{}/{}/pulls'.format(TARGET_OWNER, TARGET_REPO),
    params={
        'base': TARGET_BRANCH,
        'head': '{}:{}'.format(CANDIDATE_OWNER, CANDIDATE_BRANCH)
    },
    headers=GITHUB_HEADERS
)
existing_pulls = [pull['html_url'] for pull in pulls_body]
```

## step 5c: opening the url

finally, we can either open the existing pr, if there is one, or a new one if not:

```python
if len(existing_pulls) == 0:
    PR_URL = 'https://github.com/{}/{}/compare/{}...{}:{}?expand=1'.format(
        TARGET_OWNER,
        TARGET_REPO,
        TARGET_BRANCH,
        CANDIDATE_OWNER,
        CANDIDATE_BRANCH,
    )
elif len(existing_pulls) == 1:
    [PR_URL] = existing_pulls
else:
    raise RuntimeError('got multiple possible pull requests {!r}'.format(existing_pulls))
# TODO: support xdg-open
invoke('open', PR_URL)
```