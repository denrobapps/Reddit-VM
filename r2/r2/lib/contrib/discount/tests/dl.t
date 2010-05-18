. tests/functions.sh

title "definition lists"

rc=0
MARKDOWN_FLAGS=

SRC='
=this=
    is an ugly
=test=
    eh?'

RSLT='<dl>
<dt>this</dt>
<dd>is an ugly</dd>
<dt>test</dt>
<dd>eh?</dd>
</dl>'

if ./markdown -V | grep DL_TAG >/dev/null; then

    try '=tag= generates definition lists' "$SRC" "$RSLT"

    try 'one item with two =tags=' \
	'=this=
=is=
    A test, eh?' \
	'<dl>
<dt>this</dt>
<dt>is</dt>
<dd>A test, eh?</dd>
</dl>'
	

else
    try '=tag= does nothing' "$SRC" \
	'<p>=this=</p>

<pre><code>is an ugly
</code></pre>

<p>=test=</p>

<pre><code>eh?
</code></pre>'
	
fi

summary $0
exit $rc
