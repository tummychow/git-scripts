# review of hg absorb

https://groups.google.com/forum/#!msg/mozilla.dev.version-control/nh4fITFlEMk/ZNXgnAzxAQAJ

https://bitbucket.org/facebook/hg-experimental/src/1dc3b9cd4c1d24c8022c069c376a23a4e8aad398/hgext3rd/absorb.py

the absorb command takes the following flags:

- `-n`, `--dry-run` - don't actually do anything
- `-p`, `--print-changes` - print which changesets were modified and how they were modified
- `-i`, `--interactive` - interactively select which chunks to apply
- `-e`, `--edit-lines` - edit the changeset<->line mapping before absorbing
- `-I`, `--include` - include files matching the given mercurial file patterns
- `-X`, `--exclude` - exclude files matching the given mercurial file patterns

it analyzes the changes in your working directory and amends each of those lines into a changeset that first introduced that line. any changes that don't belong to exactly one changeset will be left untouched in the workding dir. changesets that become empty after absorption will be discarded automatically.

to identify candidate changesets, absorb combines all the following conditions:

- `::.`, ancestors of the working directory. `.` is the parent commit of the working directory, and `::` is similar to git's `..`. since the left hand of `::` is unspecified, it means "all ancestors".
- `not public()`, changesets that are local to your repo. "public" refers to a [mercurial phase](https://www.mercurial-scm.org/repo/hg/help/phases). phases are a way for separating public, shareable changes from others. commits in a local repo default to "draft", and pushing to a server makes them "public".
- `not merge()`, self-explanatory
- `absorb.maxstacksize`, a config option that determines the maximum number of changesets absorb is allowed to operate on

the absorb command invokes `absorb`, which (ignoring its optional arguments) does this:

- invoke `getdraftstack`
  - starting from the working directory's parent commit `.`, iterate backwards by parent (ie topologically) until you find a merge commit, or a commit in the public phase, or you reach the stack size limit. take the list of all commits up to and excluding that one, and put them in oldest-to-newest order
- create a `fixupstate` object with the given stack of commits
- invoke `scmutil.match` with the working dir context to get a `match` instance from `match.py`, which can be used to match against files
- if in interactive mode:
	- invoke `cmdutil.recordfilter` to let the user select chunks of the diff between the end of the stack (ie the working dir's parent `.`) and the context (ie the working dir)
	- invoke `overlaydiffcontext` to build a new context, by applying the user's selected chunks to the end of the stack
- invoke `diffwith` on the `fixupstate` object, with the context, to determine the new state of the world
- unless dry-running, invoke `apply` and `commit` on the `fixupstate` object

this walkthrough is fairly simple and demonstrates that most of the action is in `fixupstate`, which is being used like this:

- `__init__`, which just sets the stack of commits for future mutation
- `diffwith`
  - computes the list of paths modified between the end of the stack (ie the working dir's parent `.`) and the context (ie the working dir). note that "modified" comes from `basectx.status` in `context.py`, and it is separate from added (same meaning as git), removed (similar to git rm), or deleted (similar to plain rm)
  - sort the list of modified paths (this eliminates ambiguity in the event that a historical file A corresponds to present files B and C - modifications to either B or C can be propagated back into A, but not both - imo we shouldn't touch changes for such files, but that might be hard)
  - for each of those modified paths:
    - find the file context of that path in the final context (ie the working dir)
    - invoke `getfilestack` to find the list of unique historical file contexts for that path, plus the contexts in the stack that they map to
      - this works by iterating through the stack from newest to oldest and building up a list and map of contexts for the target modified path, going back to the oldest file context available that we cannot modify (eg the public commit at the root of the stack)
    - skip files that are symlinks, binary or unchanged (remember, this "unchanged" is within the stack, not between the end of the stack and the context - what this means is that there has been a change to this file in the target context, but no change in the stack to absorb it into)
    - also skip files whose data is the same between the end of the stack and the current context (eg if the file's metadata was modified but not its actual text)
    - flag all the file contexts as immutable (the first context is already immutable), this protects us if another file's history traces back to the same file that we just modified
    - create a `filefixupstate` instance with the stack of contexts for the target file and invoke `diffwith` on that against the final context of this file
      - i'm still figuring out how this function works, i think this is where most of the magic is
- `apply`
  - invoke `apply` on every `filefixupstate` that was assembled during `diffwith`
- `commit`

---

# problem statement

we are in a git repository. we are on a branch and we have uncommitted changes in the index. we want the uncommitted changes to be blended into the commits in this branch as fixup! commits (or directly into the commit if preferred).

# step 1: eligible commits

we have to first determine what commits we're allowed to fix up. to avoid the chaos of shuffling through merges, we want a strictly linear sequence. furthermore, we don't want to be fixupping into commits that belong to other branches, since we might interfere with public history. we will refer to this set of commits as the **eligible stack**.

we start, as always, by finding the current branch:

```python
HEAD = invoke('git', 'symbolic-ref', '--short', 'HEAD').strip()
```

# step 1a: exclusive ancestors of our branch

then we have to build up a rev-list expression that can find only the commits we want. we can start with this:

```python
commit_stack = invoke('git', 'rev-list', '--not', '--exclude={}'.format(HEAD), '--branches', '--not', 'refs/heads/'+HEAD).split('\n')[:-1]
```

the way this works is:

- `--branches` matches all branches
- `--not` negates all match expressions up until the next `--not`, so we surround `--branches` with two `--not` to exclude all branches
- but `--branches` also includes the branch we want, so we have to use `--exclude`, which excludes its argument from the next `--branches`
- finally we can match the branch we actually want

# step 1b: eliminating merge ancestors

now we need to add a `--topo-order` to organize the commits into a sane ordering, and we need `--parents` so that each commit lists its parents. to make sure git doesn't elide any merges from the history, we also need `--full-history --sparse`. then we eliminate the first commit in the sequence that has multiple parents, and everything afterward. finally we don't actually care about the parent information (since it's a linear chain of commits, the ancestry path is obvious), so we want to ditch the extras (note - there are some traps here because passing `--parents` enables merge parent rewriting. we're going to fix those later)

```python
commit_stack = invoke('git', 'rev-list', '--not', '--exclude={}'.format(HEAD), '--branches', '--not', 'refs/heads/'+HEAD, '--topo-order', '--full-history', '--sparse', '--parents').split('\n')[:-1]
commit_stack = list(
    map(
        lambda x: x[0],
        itertools.takewhile(
            lambda x: len(x) <= 2, # remember, we may reach the init commit which has no parent!
            map(
                lambda row: row.split(' '),
                commit_stack
            )
        )
    )
)
```

this gives us the longest possible chain of strictly linear commits that are exclusive to the current branch, from newest to oldest

# step 1c: safety valves

fixing up commits is a destructive and therefore dangerous operation in git. we want to protect the user if they invoked absorb by accident. we implement some important safety hatches:

- you can't run absorb on the default branch. this protects against you running it on master and pushing a bunch of modified commits by accident. note that "default branch" is not a trivial concept in git, because it's only defined in terms of remotes. my strategy here would be to find the default push target for the current branch, and compare that to the default branch of its remote - if they're the same, we bail. we already know how to do this from `git gpr`.
- you can't run absorb if you have merge conflicts open. this is easy to check with `git ls-files -u`.
- the eligible stack has a maximum height. if the stack exceeds the height, absorb should warn you, and truncate the stack accordingly. by default this maximum should be very low. in `hg absorb` it's 50, but i imagine a value as low as 5 is acceptable, since branches longer than that often require special care.
- the eligible stack should be composed entirely of commits you authored. this is another way to protect against accidental mutation of work that doesn't belong to you. note that we don't want to use the `--author` option to `rev-list` for this, we actually want to see commits from other authors and fail if they're present.

in addition, we want to be able to override all safety hatches with a `--force` flag, for those times where you really do just need to push that button.

# step 1d: commit authorship check

the fourth safety valve is the hardest to implement. it requires additional information about the stack, and identities in git are a complex and poorly documented business. fortunately we can always read the code in `split_ident_line` and `fmt_ident`, which reveals some important facts:

- the name and email together must always fit on a single line, git will burninate any newlines it finds along the way
- the email is always surrounded by `<` and `>`, and the stuff in between those angle brackets is the email itself. if, for some reason, there are multiple `>` in the string, git considers the email to be between the first `<`, and the first `>` after that, then it will discard everything between that `>` and the last `>` as being malformed junk.
- the username extends from the start of the string to the last non-whitespace character before the opening `<`, and it can be empty

based on these facts, we can draw an important basic conclusion: the name and email should never have newlines or angle brackets in them, and any such characters we find can be discarded as unsound.

```python
def commitify_logline(commit):
    [sha1, parent_sha1s, author_name] = commit.split('\0')
    parent_sha1s = parent_sha1s.split(' ')
    return {'commit': sha1, 'parents': parent_sha1s, 'author': author_name}

commit_stack = invoke('git', 'log', '--not', '--exclude={}'.format(HEAD), '--branches', '--not', 'refs/heads/'+HEAD, '--topo-order', '--full-history', '--sparse', '--format=tformat:%H%x00%P%x00%aE%x00').split('\0\n')[:-1]
commit_stack = list(
    itertools.takewhile(
        lambda x: len(x['parents']) <= 1,
        map(
            commitify_logline,
            commit_stack
        )
    )
)
```

let's break this down. we've moved to `log` instead of `rev-list` because log plays better with `--format` (specifically, rev-list insists on printing out a `commit <sha1>` line for each commit, in addition to whatever i write with `--format`, and that's really not what i want). our format includes the parent hashes, so we no longer need `--parents`, but all the other flags from rev-list have remained.

our format consists of the commit sha, the parent shas separated by spaces, and the author email after `.mailmap` normalization. i separate them with nulls because i'm a prude, but as discussed, emails can't contain newlines, so the nulls are probably overkill. since we're using `tformat`, we know that every format line will be terminated by a newline, so we can use the trailing null, plus the newline, to split lines into pieces.

to compare to the current user, we have to resort to some more `.mailmap` trickery. we can get the user's email from the `user.email` config variable, but then we have to pass it through the mailmap with `check-mailmap`. `check-mailmap` itself returns an identity string, which we have to destructure to retrieve the normalized email address for the current user.

```python
author_email = git_config_get('user.email').replace('<', '').replace('>', '').replace('\n', '')
author_email = invoke('git', 'check-mailmap', '<' + author_email + '>').strip()
author_email = author_email[author_email.index('<')+1:author_email.index('>', author_email.index('<'))]
```

we can check all the commits against the normalized author's email:

```python
if not all(map(lambda x: x['author'] == author_email)):
    # explode here
```

# step 1d: manual stack selection

for more precise control, the user may want to select their own stack of commits. in this case many of our safety mechanisms become irrelevant - it's the user's problem to make the right choice. we still have two requirements for soundness: the stack must end at HEAD, and the stack must not contain any merges or ancestors of merges. basically, specifying your own stack is a way to escape the branch exclusivity check.

to specify this, the user passes a revspec, and the stack is assumed to span `revspec..HEAD` (ie from HEAD, up to and not including the passed rev). we perform another log to make sure this spec is non-empty, and then truncate merges like before. we can also print a warning if we have to do the truncation (maybe the user thought that their base didn't contain any merges).

```python
if USER_BASE is not None:
    commit_stack = invoke('git', 'log', '^'+USER_BASE, 'refs/heads/'+HEAD, '--topo-order', '--full-history', '--sparse', '--format=tformat:%H%x00%P%x00%aE%x00').split('\0\n')[:-1]
    commit_stack = list(map(commitify_logline, commit_stack))
    if not all(map(lambda x: x['parents'] <= 1)):
        # print warning, and bail unless the user passed --force
    commit_stack = list(itertools.takewhile(lambda x: x['parents'] <= 1, commit_stack))
```

this invocation is pretty similar to the one before, but we've replaced the "exclude not branches not" arguments with just the user's base revision.

# step 1e: final code for stack resolution

unifying all the invocations together and cleaning stuff up gives us this:

```python
# these would come from flags or configuration
USER_BASE = None
MAX_STACK = 5
FORCE = False

# checking the default branch has been omitted
# checking for merge conflicts has been omitted

def parse_commit_log_line(log_line):
    [sha, parent_shas, author_email] = log_line.split('\0')
    return {'commit': sha, 'parents': parent_shas.split(' '), 'author': author_email}

HEAD = invoke('git', 'symbolic-ref', '--short', 'HEAD').strip()

if USER_BASE is not None:
    exclude_revs = ['^{}'.format(USER_BASE)]
else:
    exclude_revs = ['--not', '--exclude={}'.format(HEAD), '--branches', '--not']

commit_stack = invoke(
    'git', 'log',
    *exclude_revs,
    'refs/heads/{}'.format(HEAD),
    '--topo-order',
    '--full-history',
    '--sparse',
    '--format=tformat:%H%x00%P%x00%aE%x00'
).split('\0\n')[:-1]
commit_stack = list(map(parse_commit_log_line, commit_stack))

if USER_BASE is None and not FORCE:
    author_email = git_config_get('user.email').replace('<', '').replace('>', '').replace('\n', '')
    author_email = invoke('git', 'check-mailmap', '<{}>'.format(author_email)).strip()
    first_angle_bracket = author_email.index('<')
    author_email = author_email[first_angle_bracket+1:author_email.index('>', first_angle_bracket)]
    other_authors = set(map(lambda commit: commit['author'], commit_stack))
    other_authors.difference_update(author_email)
    if len(other_authors) != 0:
        raise RuntimeError('stack contains commits from foreign authors {!r}, expected only {!r}'.format(other_authors, author_email))

commit_stack = list(itertools.takewhile(lambda commit: len(commit['parents']) <= 1, commit_stack))

if len(commit_stack) > MAX_STACK and not FORCE:
    commit_stack = commit_stack[:MAX_STACK]
```

# step 2: identify affected paths

what paths are "affected" by absorb? the ones containing diffs that we want to absorb, ie the paths that have been modified between HEAD and the index. let's look at a single diff spec from `git status -z` or `git diff --name-status -z` and the possible categories of output. (all repro examples are in an empty folder in which we have run `git init && touch foo && git add foo && git commit -m init`.)

it's important to note that `git status -z` does not support all the features of `--name-status`, as discussed [here](https://marc.info/?l=git&m=141750335305994&w=2). in particular, it does not support copy detection, so although it is documented to return `C` codes, it never actually will. for best results, you should usually check the output of `git diff --name-status --find-copies-harder`, with and without `--cached`.

output | HEAD..index | index..worktree | repro example
--- | --- | --- | ---
`_M` | none | modified | `echo foo > foo`
`_D` | none | deleted | `rm foo`
`M_` | modified | none | `echo foo > foo && git add foo`
`MM` | modified | modified | `echo foo > foo && git add foo && echo foo >> foo`
`MD` | modified | deleted | `echo foo > foo && git add foo && rm foo`
`A_` | created | none | `touch bar && git add bar`
`AM` | created | modified | `touch bar && git add bar && echo bar > bar`
`AD` | created | deleted | `touch bar && git add bar && rm bar`
`D_` | deleted | none | `git rm foo`
`DM` | deleted | modified | can't reproduce this, if i try `git rm foo && touch foo` then i end up with a `D_` line and a separate `??` line
`R_` | renamed | none | `git mv foo bar`
`RM` | renamed | modified | `git mv foo bar && echo bar > bar`
`RD` | renamed | deleted | `git mv foo bar && rm bar`
`C_` | copied | none | `cp foo bar && git add bar`
`CM` | copied | modified | `cp foo bar && git add bar && echo bar > bar`
`CD` | copied | deleted | `cp foo bar && git add bar && rm bar`
`??` | untracked | untracked | `touch bar`

the part we really care about is the first character, which represents the diff between HEAD and the index. we can compute that column with `git diff --name-status -z --cached`. we can also potentially make use of the `--diff-filter=M` option to only get modified files (which is what we want, since other forms of modification cannot be absorbed anyway). finally, to get strictly normalized output, we use the plumbing `diff-index` command, and we end up with this:

```python
paths = sorted(invoke('git', 'diff-index', '--name-only', '-z', '--diff-filter=M', '--cached', HEAD).split('\0')[:-1])
```

or if we actually wanted the entire patch, we could do this. we have to be careful to pass disabling arguments so that we're isolated from the user's configuration variables. the format of a git patch is documented under the diff-generate-patch man page.

```python
invoke('git', 'diff-index', '--cached', HEAD, '--diff-filter=M', '--unified=0', '--no-color', '--diff-algorithm=default', '--word-diff=none', '--no-renames', '--full-index', '--binary', '--no-ext-diff', '--no-textconv')
```
