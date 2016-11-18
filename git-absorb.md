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

we have to first determine what commits we're allowed to fix up. to avoid the chaos of shuffling through merges, we want a strictly linear sequence. furthermore, we don't want to be fixupping into commits that belong to other branches, since we might interfere with public history. we start, as always, by finding the current branch:

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
    other_authors.difference_update([author_email])
    if len(other_authors) != 0:
        raise RuntimeError('stack contains commits from foreign authors {!r}, expected only {!r}'.format(other_authors, author_email))

commit_stack = list(itertools.takewhile(lambda commit: len(commit['parents']) <= 1, commit_stack))

if len(commit_stack) > MAX_STACK and not FORCE:
    commit_stack = commit_stack[:MAX_STACK]
```

# interlude: patch theory

let's talk, for a second, about what absorb is actually trying to do.

we have a linear sequence of commits, and we have the git index. we refer to the sequence as **the stack**: the top commit in the stack is the newest one, and the bottom commit is the oldest. the parent of the bottom commit is immutable (in most cases it'll be either a merge, or a commit that is shared with another branch). all other commits in the stack are eligible for mutation. the goal of absorb is to integrate the index into the stack by mutating the commits there. what is meant by "integrate"?

intuitively we have some idea: if a commit in the stack modified some lines, and then a hunk in the index also modified those lines, we want to integrate the latter into the former. we can formalize this concept using patch theory. the best extant implementations of patch theory are darcs and pijul; both are version control systems based on the concept. the fundamental principle of patch theory is that patches can **commute**. if i have a repository with two patches in it, and the final state of the repo is the same regardless of which patch i apply first, we say that those patches commute with each other, ie i can swap their ordering arbitrarily. in an ideal world, a repository consists entirely of patches that are all pairwise commutative with each other. there's no point ordering the patches because it doesn't matter. all orderings would have the same result.

however, not all patches commute in real life. if patch A creates a file and patch B modifies that file, then B must come after A, or otherwise B would modify a file that does not exist, which is unsound. we say that B **depends** on A; it must come after. we can represent our repository as an unconnected, directed, acyclic graph of patches. unconnected nodes can be arbitrarily reordered with respect to one another, but connected nodes are constrained by a happens-before relationship; a node cannot be reordered before another node that it is connected to (depends on).

in many cases, the dependency relationship between noncommutative nodes can be determined automatically, because only one possible ordering is valid. if only one patch exists to create a file, then all patches that modify that file must depend on it. but what if there were multiple patches that created that file? which modifying patches would depend on which creating patches? when a group of patches do not commute and there are multiple valid dependency resolutions between them, we get a **conflict**. some ordering must be enforced between these patches, because they don't commute, but multiple valid orderings are available, so the choice is ambiguous. at this point a human has to step in and resolve the conflict.

there are a variety of ways to represent conflict resolution within a patch theory system. fortunately we don't have to deal with any of those. by definition, our repository does not contain any conflicts. there already exists at least one valid patch ordering: all the commits in the stack, in order, and then the index on top. are there any other valid, equivalent orderings? asking that question reveals the purpose of absorb, which is: to find out.

we break the index patch into as many pairwise commutative patches as possible - to be specific, we break it into hunks. (hunks are the smallest unit of a patch that can still be commuted, because they are separated by at least one unchanged line, which disambiguates which one is above the other in the file. if two hunks were adjacent, then either one could go above the other, so they would conflict. likewise, if two hunks overlapped, they could be interleaved in a variety of ways, so they would conflict.) since the hunks are pairwise commutative, we can describe the absorb algorithm in terms of an individual hunk, and simply repeat it for all the hunks in the set.

we wish to find another patch that this hunk depends on, and merge the hunk into that patch. (i use the term "merge" loosely here - a merge is not really necessary since, by definition, dependent patches can be combined into one without conflicts.) to do this, we check if the hunk commutes with the top of the stack. if it cannot, then it must depend on that commit (since they don't commute with each other, and we know that the hunk comes after), so the algorithm is done. otherwise, we pop that commit off the stack and compare to the next one down. we keep doing this until either we find a commit that does not commute, or we exhaust the stack. if we exhaust the stack, then this hunk does not depend on any of the commits in the stack.

after absorbing a single hunk, we reset the stack back to its original state and continue with the next hunk. once we have gone through all the hunks in the index, we are left with the final state of the absorb algorithm. some hunks are flagged as being dependent on various commits in the stack; absorb will rewrite history to concatenate those hunks into their dependent commits. other hunks could not find a dependent, and will be left untouched in the index.

the commutation rules of patches are the key to implementing this system, and they are quite complex. broadly speaking, we have diffs (from/to text, from/to binary, and any combination thereof), adds and deletes (creating or removing files), and various metadata changes (file mode, symlink-ness, etc). we have to define which types of patch we are interested in and how they can be combined:

- we completely ignore all diff types except text-to-text. files that are binary in the index (with possible text-to-binary or binary-to-binary diffs in the stack) are ignored. if the stack contains a binary-to-text diff, text-to-text diffs are considered to be noncommutative with it.
- we ignore adds, renames and deletes in the index, because they commute too widely with other commits. broadly speaking, it doesn't make sense to absorb a deletion, since the only patch it could be absorbed into would be the last patch to change that file, and if we assume the stack consists of atomic commits, then why would we modify a file and then immediately delete it? same logic goes for adding or renaming a file.
- we ignore all metadata changes. symlinks are metadata only, so absorb treats them the same way it treats binary files. metadata changes to text are ignored by absorb, as if they were no-op patches, and are therefore commutable with text diffs.
- if we find a file being added or renamed in the stack, we imagine that patch as being decomposed into two logical parts: a pure addition/rename (adding a completely empty file, or moving one without any changes), and then a diff (adding lines to the new empty file, or modifying the file after the rename). pure renames are considered commutable with diffs; obviously additions are not.
- absorb does not require copy detection. in fact, copy detection would be detrimental to its function. suppose file A is copied to file B. file A then receives patch X and file B receives patch Y. since B is a copy of A, should Y be absorbed back into A prior to the copy? and then, if we do that, what happens if X and Y do not commute? this is an interesting question in its own right, but absorb punts on the issue by saying patches on B (eg Y) cannot be commuted through the copy back into A. B is treated as an entirely new file and, as mentioned above, diffs cannot commute with new file additions.
- deletions in the stack are also, by definition, ignored. if a file was deleted in the stack and no later commit re-added it, then our current patch of that file must be an addition, which, as mentioned, will be ignored. otherwise, another commit must have added it back in, so if we have a patch in the index that modifies it, by definition that patch cannot commute with the addition, and therefore must stop before it would reach the deletion.
- finally, text-to-text diffs commute if they are separated by at least one unchanged line (remember to adjust line offsets as required). obviously diffs cannot be commuted if they overlap. if they are adjacent, then their order is ambiguous (either one could appear above the other in the file), so they cannot be commuted either.

consolidating these rules, we find that absorb is only interested in the text-to-text diffs in the index. out of the various patch types that could appear in the stack, these can commute with: text-to-text diffs (as long as there is at least one unchanged line separating the modified regions of the two diffs), pure metadata changes, and pure renames. this gives us a formal motivation for the structure of absorb as a whole.

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

the part we really care about is the first character, which represents the diff between HEAD and the index. we can compute that column with `git diff --name-status -z --cached`. we can also potentially make use of the `--diff-filter=M` option to only get modified files (which is what we want, since other forms of modification cannot be absorbed anyway). finally, to get strictly normalized output, we use the plumbing `diff-index` command (notably, `diff-index` is immune to `diff.mnemonicPrefix`), and we end up with this:

```python
paths = sorted(invoke('git', 'diff-index', '--name-only', '-z', '--diff-filter=M', '--cached', HEAD).split('\0')[:-1])
```

or if we actually wanted the entire patch, we could do this. we have to be careful to pass disabling arguments so that we're isolated from the user's configuration variables.

```python
invoke('git', 'diff-index', '--cached', HEAD, '--diff-filter=M', '--unified=0', '--no-color', '--diff-algorithm=default', '--word-diff=none', '--no-renames', '--full-index', '--binary', '--no-ext-diff', '--no-textconv')
```

to get the patches for the stack, we could do this:

```python
# TODO: copy detection? rename detection? should we display anything else in the format? should we use minimal diff?
invoke('git', 'log', '{}^..{}'.format(commit_stack[-1]['commit'], commit_stack[0]['commit']), '--format=tformat:%H', '--unified=0', '--no-color', '--diff-algorithm=default', '--word-diff=none', '--no-renames', '--full-index', '--binary', '--no-ext-diff', '--no-textconv')
```

# TODO

- should the stack also exclude remote refs (excepting the push upstream of the current branch)? this would protect against cases where eg you fetch master and rebase onto it, but don't update your local master
- if file A is copied to file B, does a patch on file A commute backwards past the copy? even though A itself was not modified by the copy, we could argue that later patches to A cannot commute with the copy since they would have affected the copy. in this case we would have to perform copy resolution
- symbolic refs for locking to protect against multiple git absorbs being run at once
- to create a partial commit, we would need to patch a tempfile, git-hash-object -w to create a blob for that file, git-mktree to incorporate the blob into a tree object, git-commit-tree to wrap the tree into a commit object, git-update-ref to update a branch with that commit object and reflog it as appropriate

---

# git patch format

https://git-scm.com/docs/diff-generate-patch

https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html

## `diff --git`

the first line of any patch between two files takes the form of `diff --git <file1> <file2>`. the two filenames will be the same unless a file was renamed or copied - creations and deletions do _not_ use `/dev/null`.

`<file1>` will be prefixed by `a/` and `<file2>` will be prefixed by `b/`. (there are various config options like `diff.mnemonicPrefix` and `diff.noprefix` that can modify these, but if you use one of the plumbing diff commands, it will ignore those options.) if the filename contains any of tab, newline, quote or backslash, those will be backslash-escaped, and the whole thing will be quoted, so you could have headers like this:

```
diff --git "a/foo\nbar" "b/foo\nbar"
```

however, a very important caveat is those are the only characters that will be quoted out. notably, spaces are not quoted, so you can get crap like this:

```
diff --git a/ a/  b/ a/
                       ^
# there's a trailing space here
```

where did this header come from? `mkdir ' a' && touch ' a/ '`. because of all the jumbled spaces in there, this header is exceedingly difficult to parse. so as a general rule, you want to avoid parsing the names in the first line if possible. for renames and copies, git has extended header lines that can provide an unambiguous prefix-free encoding.

unfortunately there's no way around it for creation and deletion because the extended header lines do not include filenames for those cases. [linus](http://git.661346.n2.nabble.com/git-apply-git-diff-header-lacks-filename-information-for-git-diff-no-index-patch-td1134617.html#a1212949) himself regrets this mistake:

> Exactly. In order to avoid all the ambiguities, we want the filename to
match on the 'diff -' line to even be able to guess, and if it doesn't, we
should pick it up from the "rename from" lines (for a git diff), or from
the '--- a/filename'/'+++ b/filename' otherwise (if it's not a rename, or
not a git diff).

> ...

> Quite frankly, I should have doen the explicit headers as

>         "new file " <mode> SP <name>

> instead of

>         "new file mode " <mode>

one important note in linus's comment is that, for creation and deletion patches, we expect that `<file1>` and `<file2>` are the same. so hypothetically, you could parse the line using eg a backreferencing regex.

```python
>>> import re
>>> re.compile(r'a/(.+) b/\1').match('a/ a/  b/ a/ ').groups()
(' a/ ',)
```

another trick you could use here is, since the filename has to be the same on both sides, you can break the string into two pieces of equal length, and then drop the leading three characters from each half:

```python
>>> header = ' a/ a/  b/ a/ '
>>> int(len(header)/2)
7
>>> header[3:int(len(header)/2)]
' a/ '
>>> header[int(len(header)/2)+3:]
' a/ '
```

## extended headers

git patches support a lot of features that the original unified diff format wasn't intended to be aware of. it encodes these features as extended header lines. note that the format of extended header lines changes with the `--full-index` option, which is what we're going to cover here.

```
old mode <mode>
new mode <mode>
```

these header lines are used when a file's mode changes. note that git only knows three modes for files: `100644` (regular files), `100755` (executable files), and `120000` (symbolic links). it doesn't actually remember the entire unix file mode when you commit something.

```
deleted file mode <mode>
new file mode <mode>
```

these header lines indicate that the file has been created or deleted, with the given mode. as mentioned above, the file's actual path is not included here!

```
copy from <path>
copy to <path>
rename from <path>
rename to <path>
```

if you enable copy or rename detection, and git believes that this patch represents a copy or rename, then it will set the two filenames in the first line to the names before and after the patch. in addition, it also includes the names here. these header lines are important for correctly parsing the filenames, and they are also necessary to disambiguate renames from copies.

```
similarity index <number>%
dissimilarity index <number>%
```

these lines are only reported on renames/copies. git uses them to tell you how similar it thought the two files were. these aren't interesting to us.

```
index <sha1>..<sha1> <mode>
```

the index line indicates the sha1 hashes of the blobs before and after the diff. if the mode was unchanged, it will be mentioned afterwards. however, if it was changed, then it will be omitted from this line, and other extended headers will detail how the mode was affected. if you use `--full-index`, the sha1 hashes will be fully expanded to 40 characters. creations and deletions will use a hash value of zero.

although git appears to generate the extended headers in a consistent order, there's no reason they couldn't be swapped around, so be careful about that when parsing them.

## standard two-line header

at this point, we're mostly back into the territory of standard unified diffs. the next two lines represent the filename headers:

```
--- <file1>
+++ <file2>
```

the filenames used here obey the same rules as the ones in the first line - they have the `a/` and `b/` prefixes, they'll be quoted and backslash-escaped if they contain unexpected characters, etc. because each of these lines contains only one filename, they're pretty easy to parse. but remember, if the file didn't have any lines changed, these lines (and everything after them) will be omitted! so adding/removing empty files, or just modifying file modes, will not include these lines. you'll have to resort to parsing the first line in that case.

## hunk header

hunk headers are always delimited by a pair of `@@` signs. git may add more stuff after the closing `@@`, but we're going to ignore it. between the two signs are a few numbers:

```
@@ -421,0 +424,15 @@
```

the first set represents the lines before the patch, the second set represents the line after. the first number in the pair represents the starting line, and the second one is the total number of lines that the patch contains for that side (omitted, if 1).

## diff lines

after the header for a hunk come one or more lines of actual text. the lines take one of four forms:

- a leading space, followed by the actual text of the line: a line that was unaffected by the hunk. (note that, because we use `--unified=0`, we do not have to parse these)
- a `-`, followed by the text that was removed: a line that was deleted, present in the old side of the hunk but not the new one
- a `+`, followed by the text that was added: a line that was created, present in the new side of the hunk but not the old one
- a `\`, indicating a special message. git mainly uses this to say `\No newline at end of file`

after the diff lines finish, another hunk header may appear, with more diff lines, etc.

an important caveat of the diff line formulation is that git will only break hunks apart if the number of unchanged lines between them is _greater than_ the value of `--unified`. if it's less than or equal to that value, git will merge the hunks together and retain the unchanged lines in the middle. in addition, although most patch generators will put all the contiguous `-` lines together, then all the contiguous `+` lines, there's no reason to assume that's the case. so to parse a hunk's lines correctly, you need to:

- consume the unchanged lines up until the first line that's actually changed
- consume all of those changed lines (gathering them into added and removed sequences)
- consume all the unchanged lines after that
- package the groups (unchanged before, removed, added, unchanged after) into one hunk, and tweak the line counts accordingly
- save the "unchanged after" section in case it's actually the "unchanged before" section of the next hunk

# applying hunks

applying a hunk is fundamentally not that hard of a process. the reason for this is that a hunk is, by definition, contiguous - there may be unchanged lines at the start and end, but there are never any in the middle, or you'd have two separate hunks. (git may have reported multiple hunks as one, but if you parsed them correctly, then they should be split back up.) the method looks like this:

- jump to the starting line number of the hunk
- iterate over the lines in the "unchanged before" section. optionally, you can make sure these match the things in the actual text
- iterate through the removed and added sections simultaneously, replacing old lines with new lines. optionally, you can make sure the old lines match the things in the actual text that are being removed
- if the added section runs out before the removed section, just keep removing the remaining lines in the removed section
- conversely, if the removed section runs out before the added section, add those remaining lines, iterating down through the file accordingly
- iterate over the lines in the "unchanged after" section, as with the before section

and of course, you would want to make sure that the total number of lines you covered on each side matches the number recorded in the hunk header.

# commuting hunks

commuting hunks is mostly a matter of juggling line numbers. swapping two hunks around results in two new hunks which, when applied, would have the same result as the original two hunks. traditional patch tools detect commutation using the unchanged lines in the patch, and try to find those unchanged lines elsewhere in the file to see if the patch has been offset by another one that was applied earlier. since we are mostly ignoring context, our approach is slightly different.

first off, we have to make sure that the two hunks actually can commute. we confirm this by checking the range of lines added/removed (whichever is wider) in each hunk. if these ranges are not separated by at least one unchanged line, then the two hunks do not commute, and we maintain their previously existing ordering (which is something we know, in the case of absorb).

if the two change ranges are separated by at least one unchanged line, then the hunks can commute. it's important to note that we know one of the hunks comes first, and the other comes second, so when we commute them, the two resulting hunks will be in the opposite order.
