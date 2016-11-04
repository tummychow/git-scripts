# processes

use [`subprocess.run`](https://docs.python.org/3/library/subprocess.html#subprocess.run) to start processes. `check=True` will provide the generally useful behavior of exceptions on nonzero exit codes. by default, stdout/stderr are inherited (`stdout=None`, `stderr=None`). leaving stderr inherited is usually useful. to capture stdout as a string, add `stdout=subprocess.PIPE` and `universal_newlines=True`

# paths

[`pathlib`](https://docs.python.org/3/library/pathlib.html) provides a nice object-oriented interface to system paths. join them with [`/`](https://docs.python.org/3/library/pathlib.html#operators). path objects support `str()` so they can be passed directly to file-manipulating functions.

use [`pathlib.Path.resolve`](https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve) or [`os.path.realpath`](https://docs.python.org/3/library/os.path.html#os.path.realpath) for symlink resolution. pathlib does not support lexical cleaning, unfortunately (although that's fair because `..` changes meaning after symlink resolution) - use [`os.path.normpath`](https://docs.python.org/3/library/os.path.html#os.path.normpath) for that

use [`os.chdir`](https://docs.python.org/3/library/os.html#os.chdir) to change directories. note that this does not support a context manager, so you will have to un-change yourself.

# http

for http, python's urllib is about as clumsy as Net::HTTP, ie very. (building urls is much better in ruby, but response handling in python is maybe a bit better because of automatic exceptions.) python3 also forces you to confront the encoding of your response head-on, which is more correct, but annoying because it doesn't present any facility for parsing Content-Type to find it. you can do it like this:

```python
from urllib import parse, request
import json
import codecs
url = parse.urlunparse(('https', 'example.com', '/foo/bar', '', parse.urlencode({'foo': 'foo', 'bar': 'bar'}), ''))
req = request.Request(url, method='GET', headers={'Accept': 'foo', 'Authorization': 'bar'})
with request.urlopen(req) as resp:
    print(json.load(codecs.getreader('utf-8')(resp)))
```

undoubtedly if you prefer a less terrible interface, the front runner and defacto standard is [requests](https://requests.readthedocs.io/en/master/).

# other

you can bail with a stderr message using [`sys.exit`](https://docs.python.org/3/library/sys.html#sys.exit)

to manipulate users, you will need the very low-level [`pwd`](https://docs.python.org/3/library/pwd.html) module, probably with [`os.geteuid`](https://docs.python.org/3/library/os.html#os.geteuid). i respect python for exposing this interface but it's a real pain to work with euids and uids directly. see also the [setuid demystified](https://people.eecs.berkeley.edu/~daw/papers/setuid-usenix02.pdf) paper

the [`tempfile`](https://docs.python.org/3/library/tempfile.html) module provides mkstemp-based files and mkdtemp-based dirs. they support the `with` context manager, which should always be used to ensure cleanup:

```python
import tempfile
with tempfile.TemporaryFile() as f:
    f.write(b'foo')
```

[`shlex`](https://docs.python.org/3/library/shlex.html) provides shell quoting and splitting:

```python
import shlex
print(shlex.split('"foo bar" baz', posix=True))
print(' '.join([shlex.quote(s) for s in ['foo', 'bar', 'baz qux']]))
```