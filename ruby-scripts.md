# processes

use [`Open3`](https://ruby-doc.org/stdlib/libdoc/open3/rdoc/Open3.html) to handle processes, it's safer and more ergonomic than most other choices. use capture2 when stderr inheriting is desired, and capture3 otherwise. [subprocess](https://github.com/stripe/subprocess/blob/master/lib/subprocess.rb) is also worth considering if gems are allowed, as it implements pipe buffering and optional exceptions on failure

```ruby
require 'open3'
out, status = Open3.capture2(*%W[git symbolic-ref #{branch}])
abort(status) if status.exitstatus != 0
```

if you actually want stdout and stderr to be inherited to the child, use [`Kernel#system`](https://ruby-doc.org/core/Kernel.html#method-i-system)

```ruby
abort("gpg failure") unless system(*%W[gpg2 --encrypt])
```

the lowest-level call is [`Kernel#spawn`](https://ruby-doc.org/core/Kernel.html#method-i-spawn) (alias of [`Process::spawn`](https://ruby-doc.org/core/Process.html#method-c-spawn)), most of the other commands are built on top of it

[`Kernel#exec`](https://ruby-doc.org/core/Kernel.html#method-i-exec) can be used if you want to replace your process, it has a spawn-like interface but it is implemented differently underneath

avoid [backticks](https://ruby-doc.org/core/Kernel.html#method-i-60) (they always use the shell) or passing string commands to any of the above methods (also use the shell)

[`*%W[]`](https://ruby-doc.org/core/doc/syntax/literals_rdoc.html#label-Percent+Strings) is an easy way to specify an interpolated word array and splat it out into varargs, while avoiding the shell

another important gotcha: if your command only has one argument, it will be splatted into a single string and that will become a shell invocation. to avoid this, you have to pass the first argument as a 2-element array (representing the command you want to invoke, and the arg0 you want it to see). https://github.com/stripe/subprocess/blob/master/lib/subprocess.rb#L277

# paths

use [`Pathname`](https://ruby-doc.org/stdlib/libdoc/pathname/rdoc/Pathname.html) to manipulate filesystem. create dedicated `Pathname` objects for important files and chain off them. avoid stringified path manipulation (eg string concatenation), stick to `Pathname` methods like [`Pathname#join`](https://ruby-doc.org/stdlib/libdoc/pathname/rdoc/Pathname.html#method-i-join).

```ruby
require 'pathname'
Pathname.new('foo').open { |f| f.write('foo') }
```

in particular, [`cleanpath`](https://ruby-doc.org/stdlib/libdoc/pathname/rdoc/Pathname.html#method-i-cleanpath) and [`realpath`](https://ruby-doc.org/stdlib/libdoc/pathname/rdoc/Pathname.html#method-i-realpath)/[`realdirpath`](https://ruby-doc.org/stdlib/libdoc/pathname/rdoc/Pathname.html#method-i-realdirpath) should be used extensively for robust symlink and relative-path handling.

pathname does not support changing the working directory. use [`Dir::chdir`](http://ruby-doc.org/core/Dir.html#method-c-chdir) for that (preferably with a block, to unchange the directory when you're done).

# cli parsing

if you want to stick to stdlib, [`OptionParser`](https://ruby-doc.org/stdlib/libdoc/optparse/rdoc/OptionParser.html) is your only choice. be careful because it has a lot of gotchas. in particular, [`:REQUIRED`/`:OPTIONAL`](https://ruby-doc.org/stdlib/libdoc/optparse/rdoc/OptionParser.html#method-i-make_switch) indicate if a switch requires an argument, not if the switch itself is required!

if you can live with nonstd dependencies:

- for simple use cases, [slop](https://github.com/leejarvis/slop) or [trollop](https://github.com/manageiq/trollop) are established and maintained
- for bigger applications (command trees, class wrappers), [thor](https://github.com/erikhuda/thor) or [clamp](https://github.com/mdub/clamp) are better choices

features to pay attention to:

- flags can take mandatory or optional arguments
- flags and positional args can be marked as mandatory
- GNU-style long opts (`--foo=bar` or `--foo bar`)
- chainable shortopts (`-abc` == `-a -b -c`)
- shortopts with unspaced arguments (`-f foo` or `-f=foo` or `-ffoo`)
- parsing terminator (all arguments are positional after a `--`)
- smart interspersion of positional and flagged arguments
- recursive command tree support with flags for inner (non-leaf) commands
- automatic usage generation (wrapping to terminal width)
- optional envar override support
- optional command aliases or prefix invocation
- default argument values that can be distinguished from specified values
- parsing on arbitrary arrays of strings, not just argv
- automatic conversion from arg strings to typed values
- hiding commands from usage (eg for deprecation)
- automatic support for `-h`, `--help` and `help` super/subcommand, all overridable
- automatic support for `-V`/`--version`
- automatic tab completion generation for bash/zsh

# http

[`Net::HTTP`](https://ruby-doc.org/stdlib/libdoc/net/http/rdoc/Net/HTTP.html) and [`JSON`](https://ruby-doc.org/stdlib/libdoc/json/rdoc/JSON.html) can do quite a bit of heavy lifting if you don't mind a bit of verbosity. here's a complete example including persistent conn, query params and headers:

```ruby
require 'net/http'
require 'json'
Net::HTTP.start('example.com', URI::HTTPS::DEFAULT_PORT, :use_ssl => true) do |http|
  http.request(Net::HTTP::Get.new(URI::HTTPS.build({
    host: 'example.com',
    path: '/foo/bar',
    query: URI.encode_www_form({
      foo: 'foo',
      bar: 'bar',
    }),
  }), {
    Accept: 'foo',
    Authorization: 'bar',
  })) do |resp|
    abort(resp.body) unless resp.is_a?(Net::HTTPOK)
    puts JSON.parse(resp.body).inspect
  end
end
```

(note: symbols as headers was added in [2.3.0](https://github.com/ruby/ruby/commit/1a98f56ae14724611fc8f7c220e470d27f6b57e4))

many alternatives exist, varying in api, backend, and opinionated-ness:

- based on `Net::HTTP`:
  - [rest-client](https://github.com/rest-client/rest-client)
  - [httparty](https://github.com/jnunemaker/httparty)
  - [net-http-persistent](https://github.com/drbrain/net-http-persistent)
- based on `http_parser.rb`:
  - [http](https://github.com/httprb/http)
  - [em-http-request](https://github.com/igrigorik/em-http-request)
- binding directly to libcurl:
  - [typhoeus](https://github.com/typhoeus/typhoeus)
  - [patron](https://github.com/toland/patron)
  - [curb](https://github.com/taf2/curb)
- self-implemented:
  - [excon](https://github.com/excon/excon)
  - [httpclient](https://github.com/nahi/httpclient)

personally i don't want anything to do with `Net::HTTP` - if i'm going to stop using it then i want to really stop - so i'd probably resort to excon (because it's popular and pure ruby). typhoeus and http.rb both look okay as well but require linking to a c library.

# other

bail out of scripts using [`abort`](https://ruby-doc.org/core/Kernel.html#method-i-abort)

if you need the username, use [`Etc`](https://ruby-doc.org/stdlib/libdoc/etc/rdoc/Etc.html) rather than envars. supplementary groups are probably harder.

```ruby
require 'etc'
puts Etc.getpwuid.name
```

[`shellwords`](https://ruby-doc.org/stdlib/libdoc/shellwords/rdoc/Shellwords.html) provides POSIX shell word splitting, escaping and joining:

```ruby
require 'shellwords'
puts "foo\\ bar baz".shellsplit
puts %w[foo bar baz\ qux].shelljoin
```

use [`tempfile`](https://ruby-doc.org/stdlib/libdoc/tempfile/rdoc/Tempfile.html) for throwaway junk, preferably wrapping around the section that needs the tempfile. this API is not secure (implemented in pure ruby) so do not use it for tmpfiles with sensitive contents. it should really use [mkstemp](http://man7.org/linux/man-pages/man3/mkstemp.3.html) but doesn't

```ruby
require 'tempfile'
Tempfile.create('foo', '') { |f| f.write('foo') }
```