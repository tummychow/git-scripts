let's define some terminology. a _token_ is a single element of ARGV with no modification. _options_, _arguments_ and _commands_ are higher-order concepts that exist on top of tokens. an _option_ is the thing with dashes, representing some kind of key-value setting. an _argument_ is a token with no further qualification that is usually parsed by its position. finally a _command_ represents a parsing context for options and arguments, usually associated with some executable behavior.

# options

conventionally options are represented in the GNU style, ie either a single hyphen preceding a single character, or two hyphens preceding multiple characters. specifically, an option can have _names_ of the following forms:

- _short names_, which are a single alpha of either case, or a single digit
- _long names_, which are a sequence of lowercase alphas, digits and hyphens (neither the start nor the end chars can be a hyphen)

options can take _parameters_. an option can either take _no parameter_, an _optional parameter_, or a _required parameter_. usually optional parameters are treated as greedy, ie if the parser is unsure of whether to treat something as a parameter to an option, it will do so. we refer to this as the _parameter greediness rule_.

in addition an option can take the special _numeric name_, which is a single hyphen followed by any sequence of digits. the digits become the parameter for that option, so this naming scheme cannot be used for options with no parameter. this is used for things like `tail -100`.

now that all the terminology regarding options has been defined, we can list the various forms by which options can be expressed and how parameters can be passed to them:

- `-a`, short name `a` with no parameter
- `-a=foo`, short name `a` with parameter `foo`
- `-a foo`, which could be parsed two ways:
  - short name `a` with parameter `foo` (if `a` takes a parameter)
  - short name `a` with no parameter, followed by argument `foo` (if `a` does not take a parameter)
- `-abc`, which could be parsed in various ways, depending on which of these options take parameters:
  - short name `a` with parameter `bc` (if `a` takes a parameter - this is the most common interpretation)
  - short name `a` with no parameter, followed by short name `b` with parameter `c` (if `a` does not take a parameter but `b` does)
  - short names `a`, `b` and `c`, all with no parameters (if neither `a` nor `b` take parameters)
  - rejected as ambiguous
- `-ab=foo`, short name `a` with no parameter, and short name `b` with parameter `foo` (note: this could also be parsed as short name `a` with parameter `b=foo`. however, the user usually places the equals sign as a strong disambiguator for where the parameter is, so such an interpretation would be very surprising.)
- `-ab foo`, which could be parsed in various ways, depending on which of these options take parameters:
  - short name `a` with parameter `b`, followed by argument `foo` (if `a` takes a parameter)
  - short name `a` with no parameter, followed by short name `b` with parameter `foo` (if `a` does not take a parameter but `b` does)
  - short names `a` and `b` with no parameters, followed by argument `foo` (if neither `a` nor `b` take parameters)
- `--foo`, long name `foo` with no parameter
- `--foo=bar`, long name `foo` with parameter `bar`
- `--foo bar`, which could be parsed two ways:
  - long name `foo` with parameter `bar` (if `foo` takes a parameter)
  - long name `foo` with no parameter, followed by argument `bar` (if `foo` does not take a parameter)
- `-123`, which could be parsed in various ways:
  - short name `1` with parameter `23` (if `1` takes a parameter)
  - short name `1` with no parameter, followed by short name `2` with parameter `3` (if `1` does not take a parameter but `2` does)
  - short names `1`, `2` and `3`, all with no parameters (if neither `1` nor `2` take parameters)
  - numeric name with parameter `123` (if `1` is not a known short name)

as shown above, there are three major types of ambiguity in parsing options:

- if multiple short names are chained together, should they all be treated as options, or should some characters be treated as the parameter to another option? in this case, we typically apply the parameter greediness rule, as mentioned above: the first option (from left to right) that can take a parameter is treated as doing so, and consumes all the remaining characters.
- if an option takes an optional parameter, but none was specified in its token, should it consume the next token as its argument? again applying the parameter greediness rule, the answer is generally yes. (however, type sensitive parsing may allow the parser to detect that the next token could not be a parameter to that option. more on this later)
- if multiple digit short names are chained together, do they represent multiple options, or a numeric name? the typical solution to this ambiguity is to simply diasllow parsers that can encounter it - ie a parser can only contain one option that takes the numeric name, and if there is such an option, then no options are allowed to use digit short names. however, it could also be legal to allow it, if the numeric name had lower precedence than any existing digit short names (ie numeric names can only be accepted if they start with digits that are not a known short name).

all of the discussed forms assume that an option takes exactly one parameter, but what if it can take multiple? for example, bubblewrap has many options that take exactly two parameters (eg `bwrap --ro-bind /usr /usr ...`). there are also commands whose options can take an arbitrary number of parameters. an excellent example is find, whose exec option consumes parameters up to and including a `;` or `+` (eg `find -exec rm {} +`).

these can be represented as generalizations of the above. a given option can take a range of parameter arities. zero parameters is the none case, 0-1 is the optional case, and 1 is the required case. further representations then become possible, such as `--ro-bind`'s 2 parameters, or `-exec`'s 1-inf parameters.

some options are order-sensitive. for example, the order in which bubblewrap performs filesystem and bind mounts is determined by the order of the options that specify them. therefore, although options are generally key-value pairs, they generally need to be order-preserving.

a common pattern for options that take no parameter is to treat them as boolean switches and autogenerate a false version of the option. if such an option has a long name, another option will be generated with the same long name, prefixed with `no-`, which turns the option off.

option parsing also needs to be aware of the possibility of repeated options. some common examples of how this is used:

- if various boolean on and off options are supplied one after another, the last one usually wins
- a verbosity or forcing option can often be specified multiple times to represent increased effect, eg `rsync -vv`. although this option takes no parameter from the user's perspective, it is actually an integer (representing the number of times it was specified).
- some options that take parameters can be specified repeatedly to build up a list

# arguments

broadly speaking, arguments are "tokens that aren't options or parameters to options" (with the exception of commands, which will be discussed later). generally arguments are parsed by order, with facility for type sensitive overloading - that is to say, there can be multiple different argument configurations, as long as they can be disambiguated by the types of their contents. each such configuration is referred to as a _signature_. just like how a c++ compiler tries to resolve an overloaded function call by matching the types of the arguments to the signatures of the function's definitions, the parser will attempt to parse the arguments according to each signature until it can identify which one applies.

parsing tokens as arguments can be explicitly requested via the `--` token. this token is known as the _argument delimiter_. all tokens following this one are treated as arguments, never as options, option parameters or command initiators (see below). an argument signature can choose to treat the `--` token specially so that it can distinguish arguments coming before it from arguments coming after.

a good example of a complex argument signature is `git checkout`. normally checkout treats its first positional argument as a commit ref, optionally followed by specific paths in the working tree to check out. if the first positional argument is not a commit ref, it is skipped and that argument is treated as another path instead. in addition, all arguments after the delimiter are treated as paths. for example:

- `git checkout master foo` could be parsed in two ways:
  - if `master` is a known commit ref, then it will be parsed as such, and `foo` will be a path
  - if `master` is not a commit ref, git will treat both `master` and `foo` as paths
- `git checkout -- master foo` would always treat both `master` and `foo` as paths

note how `master` is the first positional argument in both examples. however, git treats it differently depending on whether it came before or after the argument delimiter.

some commands also change their valid argument signatures based on the presence or absence of an option. for example, if `git diff` is invoked with `--no-index`, the only valid argument signature that remains is two file paths. the tricky part is that the option might appear after the arguments themselves.

# commands and parsing contexts

at any given time, the parser has a _context_, which consists of two parts: a set of valid argument signatures, and a set of valid options (referred to as an _option scope_). the context defines what arguments and options are valid, and when. however, complex command line programs often have a suite of behaviors, each demanding its own context. the selection of behavior by the user is implemented in terms of commands.

every command has one or more _initiators_. these represent tokens that, instead of being parsed as arguments, will instead be treated as triggers to switch to the context of the matching command. the set of initiators that are in the current context represent the child commands of the command whose context is currently being parsed. thus the commands form a directed acyclic graph.

# combinator parsing for command line interfaces

finally with all of this in mind, we can consider the parsing interface as a a whole. we will apply a classic concept from functional programming, parser combinators, to build up a total cli parsing solution.

the theory of parser combinators is that we can define a number of small parsers, each of which correctly parses a single form of input, and then use higher-order functions to combine small parsers together. a parser can be thought of as a state machine that consumes tokens one at a time, transitioning through three states:

- initial: ready to consume input and parse a new object from scratch.
- ongoing: parsing an object, but still capable of consuming more input for it.
- fatal: the object that was being parsed has been invalidated, and parsing cannot continue.

these states can move through a number of edges, triggered by receiving input, and depending on whether that input was consumed by the parser:

- initial->initial (consuming input) or ongoing->initial (consuming or rejecting input): this is the success edge, in which a fully parsed object (whatever that entails) is returned, along with any tokens that were not consumed. tokens could be left unconsumed if, for example, a parser was in a currently valid state and then received an invalid token. the invalid token could not be parsed, but since the current state was already valid, it would be returned as is, along with the invalid token, which would be safe for retry elsewhere. essentially, the invalid token becomes an EOI (see below) and is then rejected.
- initial->initial (rejecting input): this is the rejection edge, in which the parser ignores the incoming input with an error response. since it has not consumed any other input prior to this error, it remains in a sound state and the rejected token can be retried. typically the parent combinator will do so with other parsers, but if no other parser is available, then the error would be escalated to the user.
- initial->ongoing (consuming input) or ongoing->ongoing (consuming input): this is the incompleteness edge, in which the parser was able to consume input, and is still able to consume more input. any time that a parser could potentially consume more input, it should return incompleteness, even if its current state is potentially valid as is. the _end of input token_ (EOI) is used to signal that no further input is forthcoming, which forces a parser to resolve its current state to either success or failure. returning incompleteness in response to EOI would be interpreted as a failure, and reported accordingly.
- ongoing->fatal (rejecting input): this is the failure edge, in which a parser has consumed some input expecting to build up a parsed object, but now finds an input that invalidates what it has already consumed. since it has already consumed input, it is generally unsound to retry this failed token elsewhere, and the parent combinator should escalate the error details to the user (although there are situations, such as the argument signature combinator detailed below, in which failure of one child is not fatal to the combinator as a whole). this could be compared to nom's [`error!`](https://github.com/Geal/nom/wiki/Error-management#early-returns), which prevents backtracking the failed token.
- fatal->initial: this is the special reset edge, which can be requested by the caller in the event that failure is considered to be recoverable. unless this edge is invoked, fatal is a terminal state in which no further transitions are possible.

another way of interpreting this state machine is that, any time a token is consumed, the state becomes either initial or ongoing, depending on whether that token is sufficient to finalize a parsed object. any time a token is rejected, the state becomes either initial or fatal, depending on whether the previous state was initial or ongoing.

in code, we represent these as four possible return values: success (containing the parsed value and, optionally, any leftover tokens), incompleteness (optionally containing a hint of how many more tokens are needed), rejection (containing a reason for the rejection) and failure (containing a reason for the failure).

on top of individual parsers, which parse a single thing to completion (a single option and its parameters, or a single argument), we build combinators that merge these things together. a common theme with combinators is that they do not return intermediate successes from their children, but instead remember those successes for later, returning incompleteness instead. the total batch of all successes is only yielded upon a valid EOI (ie an EOI is received while the combinator's overall state allows for termination), or when no further input can be consumed. a higher-order combinator will typically treat its child combinators as "one success only" - ie after a child combinator returns a success with its batched results, the parent combinator understands that no further input should be passed to it, and doing so would be an error.

## parsing options

to parse options correctly, each individual option gets its own parser, which we will simply refer to as an _individual option parser_. rather than consuming raw tokens from argv, this parser recognizes the option's name(s), followed by a stream of inputs representing its parameters. signalling metacharacters, like equals signs or leading hyphens, are not included in this representation. (omitting these metacharacters makes it much easier for the parent combinator to implement behaviors like short name chaining.) an individual option parser can be queried for a list of all the name forms it accepts; this is useful for the parent. this parser's behavior can be characterized like so:

- if the input does not match any of this option's names, reject it
- if it does match a name, then consume that input, and then:
  - if this option cannot accept any more parameters, return success
  - otherwise return incomplete, signalling that more parameters can be consumed. then, on subsequent inputs:
    - if this input can be parsed as a parameter for this option, then consume it, and either return success (if no more parameters can be consumed) or incomplete
    - if this input cannot be parsed as a parameter for this option, then either return success with that input left unconsumed (if this option has received enough parameters to be valid, then we will acknowledge as such without consuming this input) or failure

given a series of individual option parsers, we can define a combinator that knows how to drive those parsers and parse out the metacharacters that get in their way. this combinator also implements behaviors like short name chaining, and is known as the _option scope combinator_ (because it contains all the individual parsers for a given option scope). it takes several individual option parsers as arguments, checks their names to make sure none of them conflict, and loads those names into a hashtable. it then consumes tokens like so:

- if the token could not be a valid option, then reject it. (valid options must be either one hyphen followed by at least one valid short name followed by an arbitrary string, or two hyphens followed by a valid long name then possibly an equals sign and an arbitrary string.) **NOTE**: the option scope combinator may return rejection for a token after returning incompleteness (ie from the ongoing state). this is technically a violation of the previously discussed parser state machine. another way to support this might be to return an "empty success" after an option is parsed, indicating that the combinator is ready for a new option to begin, but not actually returning what option it finished parsing.
- if the token has a single leading hyphen, then:
  - if all its subsequent characters are digits, and we have a numeric name option, then pass all those digits to its parser.
  - otherwise, find the parser that accepts the first character after the hyphen as a name (if there is no such parser then return a failure). what does it return?
    - success: the option didn't take any parameters. if the next character is an equals sign, then the user mistakenly passed it one, so return a failure. otherwise repeat with the next character.
    - incomplete: it's waiting for a parameter. here is the algorithm for finding one:
      - if the next character is an equals sign, then the user clearly intended for everything after that to be the parameter, so pass that to the option parser.
      - if one of the characters AFTER the next one is an equals sign (eg `-ab=foo`), then the user passed a parameter but it was not intended for this option, so we have nothing to offer. pass EOI to the option parser and see how it responds.
      - if there are characters after this option and none of them are equals signs, try passing all of them to the option parser.
      - if no characters are left in this token, then return the incomplete to the parent parser. wait for the parent to give us another token and pass that to the option parser (even if it's EOI).
      - once we've passed something to the option parser, we will deal with whatever it returns - either another incomplete (repeat as above), a success (if there are still characters in this token, then parse those as further options) or a failure (which we will escalate to the parent).
    - once we're done parsing the options in this token, we'll return a success containing all of their parsed objects, and await further input.
- if the token has two leading hyphens, then we have a long name. find it (excluding the part after the equals sign if any) and pass it to the appropriate parser. from here, the parameter-passing heuristic is simliar to for short names.

as discussed above, the option scope combinator batches up the parsed options of all its children before returning them. this allows it to implement coalescing behavior for repeated options, or failure when mandatory options are omitted.

## parsing arguments

parsing an individual argument is pretty easy - delegate to whatever type-specific parser the argument's type supports. we call this an _argument parser_. parsing entire signatures is more interesting. each signature can be thought of as a state machine that remembers what argument forms are currently valid, and and enforces an order of preference over them. the _signature combinator_ implements this state machine. it receives a token (possibly including the argument delimiter) and tries the token with each individual argument parser that could currently apply. if the result was a success, it stores the success for later, and executes any state transitions if necessary (ie changing the list of argument parsers that will be valid for the next input). if it was a rejection, it tries the next argument parser in precedence order (and if there is none, then it returns failure). if it was incompleteness, it escalates that to the caller until either success (stored, as before) or failure (escalated) is returned. (note: incompleteness makes it much more complicated to disambiguate paths on the state machine. it might be worthwhile to disallow this, ie every argument parser must return either success or rejection.) as discussed, the signature combinator returns the batch of all its arguments in order, not one at a time. this will be useful for its parent combinator.

given multiple signature combinators and a precedence ordering between them, we implement the _signature multiplexer_. this combinator is implemented by arbitrary lookahead multiplexing; that is, it drives all child signature parsers simultaneously. (this is similar to parsec's choice combinator, where every argument was wrapped in the try combinator.) each child is in one of three states: valid (ready to consume more input), invalid (it returned an error, which is stored, and cannot consume more input) or complete (it returned a success, which is stored, and also cannot consume any more input). these three states represent the "one success only" condition that was mentioned above - if a child signature is in the completed state, then the next input token must be EOI, or else that child is invalidated.

upon receiving input, the multiplexer first checks for any completed children. if the current input is not EOI, then it invalidates all of those completed children. then, for each valid child, the multiplexer passes the input token to that child and observes the output (hence the name multiplexer). if it's a rejection or failure, then the input sequence so far does not match that signature, so the child is invalidated. (we are assuming that ultimately one signature must win, ie must consume all input, so rejection and failure are equivalent.) if it's a success, then the success output is stored (in case this signature ends up being the winner), and the signature becomes successful. if the child returns incompleteness, then it remains in the valid state.

once all valid children have received the input, the multiplexer decides what to return next. as long as there is at least one valid child, it must return incomplete (since it is waiting for more input to distinguish that valid child from any other valid or completed children). if no more valid children remain, then it returns the first remaining completed child's output, in precedence order. if no completed children remain either, then it identifies the child(ren) that were invalidated by this specific input, and returns the failure of the first of those children, in precedence order. (this way, the child whose failure gets returned is the one that stayed valid the longest, ie the one that was invalidated most recently.)

the multiplexer could potentially be modified to support signature invalidation during parsing, ie the parent combinator could explicitly order it to mark a signature as invalid. this could be used to implement a variety of higher-order behaviors. for example, if a given signature is only valid with a certain option, then the appearance of that option would invalidate all other signatures, and if EOI was encountered before that option was found, then the corresponding signature could be invalidated. with this in mind, the multiplexer should be constructed with the maximally general set of signatures - rather than adding signatures later, only removal is supported.

it's important to note that, if an individual signature is represented by a state machine, and the multiplexer is the union of multiple signatures, then it itself could be thought of as a single, larger state machine. so the signature combinator and the multiplexer could hypothetically be combined into one, implementing a nondeterministic finite automaton over individual argument parsers. (implementing an NFA makes it easier to handle things like multi-token arguments, where the correct transition could be ambiguous until more tokens are consumed.) one quirk of combining the state machines is that removing a signature would become a lot harder. nevertheless this is an important similarity to keep in mind when implementing the signature combinator.

## parsing commands

to parse a command, we first start with the _option-argument combinator_. this combinator takes an option scope combinator and a signature multiplexer as arguments. the behavior of this combinator is fairly simple: it passes tokens to the option scope combinator first, and if the option scope starts consuming them, then the combinator will continue driving the scope until either a failure (which gets escalated) or a rejection/empty success followed by rejection (see bolded note above). if the option scope combinator rejects the token, it will be passed to the multiplexer. the option-argument combinator keeps doing this until either of its children returns a failure. this simple behavior is sufficient for parsing a single command. if the command wishes to implement option-specific argument signatures, those can be implemented at this level (by first passing EOI to the option scope combinator to get the parsed options, and then applying any signature invalidation to the multiplexer, and then passing EOI to it to see what signatures remain). you could also disable option-argument interspersion at this level, although i can't imagine why you would want that (once the option scope combinator rejects a token, you would just disable it altogether and only use the multiplexer from then on).

for parsing a complex multi-command application, we introduce the _command tree combinator_. this combinator contains a directed acyclic graph of option-argument combinators. each edge of the tree is associated with a set of initiators. (for best results, the internal nodes' combinators should probably have no argument signatures, since ambiguity between arguments and initiators would be very difficult to resolve predictably. this could be enforced if desired.) the combinator keeps track of what node in the tree it is currently on and feeds tokens to that node's combinator, with two exceptions. first, if the token matches an initiator for an adjacent edge, then the current node receives an EOI, and if that does not fail, the combinator moves along the matching edge, to the next node. second, if the token is the argument delimiter, all initiators are deleted (essentially preventing any further command transitions). the success output of the command tree combinator is a fully parsed command object, indicating what command it is, and containing the results of that command's option-argument combinator.

# application building additions

the above details how to parse a command line. now we must consider the details of how the programmer applies the parser to their specific application, and what conveniences are provided by whatever interface the parser exposes. some things to consider:

- programmer should be able to associate help details with any option, argument signature or command, resulting in automatically generated help output
- optional disabling of help output for individual options, commands or argument signatures (useful for deprecating old invocation patterns)
- help output should automatically wrap to the terminal width if detectable, and to a user-configurable default if not (also, terminal detection should be provided as a convenience to the programmer if they need it to solve other problems, eg color output)
- automatic generation of `--help` and `-h` options
- automatic generation of `help` subcommand at any point in the tree (ie `root help foo bar`, `root foo help bar`, `root foo bar help` should all provide equivalent help output)
- automatic generation of `--version` and `-V` options, if such metadata is available from the build system
- optional aliases or shortest-prefix invocation for command initiators and option names (the program should also be able to tell which alias it was actually invoked with, although branching on this would be poor style)
- optional environment variable or config file overrides (this is useful for headless invocation, where passing ARGV would necessitate a shell script wrapper. if all necessary options/args/commands could be passed by envars instead, the program could be invoked directly from its executable, with no ARGV. of course, this is easier with simple applications that have mostly option-driven behavior, with little to no arguments or commands.)
- automatic bash/zsh/fish tab completion and manpage generation, as a build output if the tooling supports it (note that the help output alone is probably not enough to populate an entire manpage, so it might be worth having a way of attaching short help for the help output and long help for the manpage)
- optional command invocation driven by the parser (ie instead of having to determine which command the user passed, just associate a function with each command, and let the parser call the right one. this avoids the need for nested if/switch/match statements in deep command trees, since the parser already knows which command won and can resolve the conditionals for you. it's important that this feature should be optional, since shallow command trees are often easier to execute manually.)
- generation of parser and associated structures at compile time (ie i should not have to run the program to find out that i specified an ambiguous option scope; that should cause my build to fail)
- automatic conversion of option parameters and arguments to typed objects in the host language (what is the success return type of an option scope combinator or a signature combinator? remember, order must be preserved!)
- parser can be invoked on arbitrary string arrays, not just on ARGV (but a simple wrapper should be provided that uses ARGV and terminates the program on failure, since that will be the most common use case)

libraries worth comparing to:

- python's [click](http://click.pocoo.org), made for flask
- [cobra](https://github.com/spf13/cobra), the library behind kubectl