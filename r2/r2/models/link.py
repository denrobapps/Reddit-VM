# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is Reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
#
# All portions of the code written by CondeNet are Copyright (c) 2006-2010
# CondeNet, Inc. All Rights Reserved.
################################################################################
from r2.lib.db.thing import Thing, Relation, NotFound, MultiRelation, \
     CreationError
from r2.lib.db.operators import desc
from r2.lib.utils import base_url, tup, domain, title_to_url
from r2.lib.utils.trial_utils import trial_info
from account import Account, DeletedUser
from subreddit import Subreddit
from printable import Printable
from r2.config import cache
from r2.lib.memoize import memoize
from r2.lib.filters import profanity_filter, _force_utf8
from r2.lib import utils
from r2.lib.log import log_text
from mako.filters import url_escape
from r2.lib.strings import strings, Score

from pylons import c, g, request
from pylons.i18n import ungettext, _
from datetime import datetime
from hashlib import md5

import random, re

class LinkExists(Exception): pass

# defining types
class Link(Thing, Printable):
    _data_int_props = Thing._data_int_props + ('num_comments', 'reported')
    _defaults = dict(is_self = False,
                     over_18 = False,
                     reported = 0, num_comments = 0,
                     moderator_banned = False,
                     banned_before_moderator = False,
                     media_object = None,
                     has_thumbnail = False,
                     promoted = None,
                     pending = False,
                     disable_comments = False,
                     selftext = '',
                     ip = '0.0.0.0')
    _essentials = ('sr_id',)
    _nsfw = re.compile(r"\bnsfw\b", re.I)

    def __init__(self, *a, **kw):
        Thing.__init__(self, *a, **kw)

    @classmethod
    def by_url_key(cls, url):
        maxlen = 250
        template = 'byurl(%s,%s)'
        keyurl = _force_utf8(base_url(url.lower()))
        hexdigest = md5(keyurl).hexdigest()
        usable_len = maxlen-len(template)-len(hexdigest)
        return template % (hexdigest, keyurl[:usable_len])

    @classmethod
    def _by_url(cls, url, sr):
        from subreddit import Default
        if sr == Default:
            sr = None

        url = cls.by_url_key(url)
        link_ids = g.urlcache.get(url)
        if link_ids:
            links = Link._byID(link_ids, data = True, return_dict = False)
            links = [l for l in links if not l._deleted]

            if links and sr:
                for link in links:
                    if sr._id == link.sr_id:
                        return link
            elif links:
                return links

        raise NotFound, 'Link "%s"' % url

    def set_url_cache(self):
        if self.url != 'self':
            key = self.by_url_key(self.url)
            link_ids = g.urlcache.get(key) or []
            if self._id not in link_ids:
                link_ids.append(self._id)
            g.urlcache.set(key, link_ids)

    def update_url_cache(self, old_url):
        """Remove the old url from the by_url cache then update the
        cache with the new url."""
        if old_url != 'self':
            key = self.by_url_key(old_url)
            link_ids = g.urlcache.get(key) or []
            while self._id in link_ids:
                link_ids.remove(self._id)
            g.urlcache.set(key, link_ids)
        self.set_url_cache()

    @property
    def already_submitted_link(self):
        return self.make_permalink_slow() + '?already_submitted=true'

    def resubmit_link(self, sr_url = False):
        submit_url  = self.subreddit_slow.path if sr_url else '/'
        submit_url += 'submit?resubmit=true&url=' + url_escape(self.url)
        return submit_url

    @classmethod
    def _submit(cls, title, url, author, sr, ip):
        from r2.models import admintools

        l = cls(_ups = 1,
                title = title,
                url = url,
                _spam = author._spam,
                author_id = author._id,
                sr_id = sr._id,
                lang = sr.lang,
                ip = ip)
        l._commit()
        l.set_url_cache()
        if author._spam:
            admintools.spam(l, banner='banned user')
        return l

    @classmethod
    def _somethinged(cls, rel, user, link, name):
        return rel._fast_query(tup(user), tup(link), name = name,
                               timestamp_optimize = True)

    def _something(self, rel, user, somethinged, name):
        try:
            saved = rel(user, self, name=name)
            saved._commit()
        except CreationError, e:
            return somethinged(user, self)[(user, self, name)]

        rel._fast_query_timestamp_touch(user)
        return saved

    def _unsomething(self, user, somethinged, name):
        saved = somethinged(user, self)[(user, self, name)]
        if saved:
            saved._delete()
            return saved

    @classmethod
    def _saved(cls, user, link):
        return cls._somethinged(SaveHide, user, link, 'save')

    def _save(self, user):
        return self._something(SaveHide, user, self._saved, 'save')

    def _unsave(self, user):
        return self._unsomething(user, self._saved, 'save')

    @classmethod
    def _clicked(cls, user, link):
        return cls._somethinged(Click, user, link, 'click')

    def _click(self, user):
        return self._something(Click, user, self._clicked, 'click')

    @classmethod
    def _hidden(cls, user, link):
        return cls._somethinged(SaveHide, user, link, 'hide')

    def _hide(self, user):
        return self._something(SaveHide, user, self._hidden, 'hide')

    def _unhide(self, user):
        return self._unsomething(user, self._hidden, 'hide')

    def link_domain(self):
        if self.is_self:
            return 'self'
        else:
            return domain(self.url)

    def keep_item(self, wrapped):
        user = c.user if c.user_is_loggedin else None

        if not c.user_is_admin:
            if self._spam and (not user or
                               (user and self.author_id != user._id)):
                return False

            #author_karma = wrapped.author.link_karma
            #if author_karma <= 0 and random.randint(author_karma, 0) != 0:
                #return False

        if user and not c.ignore_hide_rules:
            if user.pref_hide_ups and wrapped.likes == True:
                return False

            if user.pref_hide_downs and wrapped.likes == False:
                return False

            if wrapped._score < user.pref_min_link_score:
                return False

            if wrapped.hidden:
                return False

        # Uncomment to skip based on nsfw
        #
        # skip the item if 18+ and the user has that preference set
        # ignore skip if we are visiting a nsfw reddit
        #if ( (user and user.pref_no_profanity) or
        #     (not user and g.filter_over18) ) and wrapped.subreddit != c.site:
        #    return not bool(wrapped.subreddit.over_18 or 
        #                    wrapped._nsfw.findall(wrapped.title))

        return True

    # none of these things will change over a link's lifetime
    cache_ignore = set(['subreddit', 'num_comments', 'link_child']
                       ).union(Printable.cache_ignore)
    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        if wrapped.promoted is not None:
            s.extend([getattr(wrapped, "promote_status", -1),
                      getattr(wrapped, "disable_comments", False),
                      wrapped._date,
                      c.user_is_sponsor,
                      wrapped.url, repr(wrapped.title)])
        if style == "htmllite":
             s.extend([request.get.has_key('twocolumn'),
                       c.link_target])
        elif style == "xml":
            s.append(request.GET.has_key("nothumbs"))
        s.append(getattr(wrapped, 'media_object', {}))
        return s

    def make_permalink(self, sr, force_domain = False):
        from r2.lib.template_helpers import get_domain
        p = "comments/%s/%s/" % (self._id36, title_to_url(self.title))
        # promoted links belong to a separate subreddit and shouldn't
        # include that in the path
        if self.promoted is not None:
            if force_domain:
                res = "http://%s/%s" % (get_domain(cname = False,
                                                   subreddit = False), p)
            else:
                res = "/%s" % p
        elif not c.cname and not force_domain:
            res = "/r/%s/%s" % (sr.name, p)
        elif sr != c.site or force_domain:
            res = "http://%s/%s" % (get_domain(cname = (c.cname and
                                                        sr == c.site),
                                               subreddit = not c.cname), p)
        else:
            res = "/%s" % p

        # WARNING: If we ever decide to add any ?foo=bar&blah parameters
        # here, Comment.make_permalink will need to be updated or else
        # it will fail.

        return res

    def make_permalink_slow(self, force_domain = False):
        return self.make_permalink(self.subreddit_slow,
                                   force_domain = force_domain)
    
    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.pages import make_link_child
        from r2.lib.count import incr_counts
        from r2.lib.media import thumbnail_url
        from r2.lib.utils import timeago
        from r2.lib.template_helpers import get_domain
        from r2.models.subreddit import FakeSubreddit
        from r2.lib.wrapped import CachedVariable

        # referencing c's getattr is cheap, but not as cheap when it
        # is in a loop that calls it 30 times on 25-200 things.
        user_is_admin = c.user_is_admin
        user_is_loggedin = c.user_is_loggedin
        pref_media = user.pref_media
        pref_frame = user.pref_frame
        pref_newwindow = user.pref_newwindow
        cname = c.cname
        site = c.site

        saved = Link._saved(user, wrapped) if user_is_loggedin else {}
        hidden = Link._hidden(user, wrapped) if user_is_loggedin else {}
        trials = trial_info(wrapped)

        #clicked = Link._clicked(user, wrapped) if user else {}
        clicked = {}

        for item in wrapped:
            show_media = False
            if not hasattr(item, "score_fmt"):
                item.score_fmt = Score.number_only
            item.pref_compress = user.pref_compress
            if user.pref_compress and item.promoted is None:
                item.render_css_class = "compressed link"
                item.score_fmt = Score.points
            elif pref_media == 'on' and not user.pref_compress:
                show_media = True
            elif pref_media == 'subreddit' and item.subreddit.show_media:
                show_media = True
            elif item.promoted and item.has_thumbnail:
                if user_is_loggedin and item.author_id == user._id:
                    show_media = True
                elif pref_media != 'off' and not user.pref_compress:
                    show_media = True

            item.over_18 = bool(item.over_18 or item.subreddit.over_18 or
                                item._nsfw.findall(item.title))
            item.nsfw = item.over_18 and user.pref_label_nsfw

            if user.pref_no_profanity and item.over_18 and not c.site.over_18:
                item.thumbnail = ""
            elif not show_media:
                item.thumbnail = ""
            elif item.has_thumbnail:
                item.thumbnail = thumbnail_url(item)
            elif item.is_self:
                item.thumbnail = g.self_thumb
            else:
                item.thumbnail = g.default_thumb

            item.score = max(0, item.score)

            item.domain = (domain(item.url) if not item.is_self
                           else 'self.' + item.subreddit.name)
            item.urlprefix = ''
            item.saved = bool(saved.get((user, item, 'save')))
            item.hidden = bool(hidden.get((user, item, 'hide')))
            item.clicked = bool(clicked.get((user, item, 'click')))
            item.num = None
            item.permalink = item.make_permalink(item.subreddit)
            if item.is_self:
                item.url = item.make_permalink(item.subreddit, force_domain = True)

            # do we hide the score?
            if user_is_admin:
                item.hide_score = False
            elif item.promoted and item.score <= 0:
                item.hide_score = True
            elif user == item.author:
                item.hide_score = False
            elif item._date > timeago("2 hours"):
                item.hide_score = True
            else:
                item.hide_score = False

            # store user preferences locally for caching
            item.pref_frame = pref_frame
            item.newwindow = pref_newwindow
            # is this link a member of a different (non-c.site) subreddit?
            item.different_sr = (isinstance(site, FakeSubreddit) or
                                 site.name != item.subreddit.name)

            if user_is_loggedin and item.author_id == user._id:
                item.nofollow = False
            elif item.score <= 1 or item._spam or item.author._spam:
                item.nofollow = True
            else:
                item.nofollow = False

            if c.user.pref_no_profanity:
                item.title = profanity_filter(item.title)

            item.subreddit_path = item.subreddit.path
            if cname:
                item.subreddit_path = ("http://" + 
                     get_domain(cname = (site == item.subreddit),
                                subreddit = False))
                if site != item.subreddit:
                    item.subreddit_path += item.subreddit.path
            item.domain_path = "/domain/%s" % item.domain
            if item.is_self:
                item.domain_path = item.subreddit_path

            # attach video or selftext as needed
            item.link_child, item.editable = make_link_child(item)

            item.tblink = "http://%s/tb/%s" % (
                get_domain(cname = cname, subreddit=False),
                item._id36)

            if item.is_self:
                item.href_url = item.permalink
            else:
                item.href_url = item.url

            # show the toolbar if the preference is set and the link
            # is neither a promoted link nor a self post
            if pref_frame and not item.is_self and not item.promoted:
                item.mousedown_url = item.tblink
            else:
                item.mousedown_url = None

            item.fresh = not any((item.likes != None,
                                  item.saved,
                                  item.clicked,
                                  item.hidden,
                                  item._deleted,
                                  item._spam))

            item.is_author = (user == item.author)

            # bits that we will render stubs (to make the cached
            # version more flexible)
            item.num = CachedVariable("num")
            item.numcolmargin = CachedVariable("numcolmargin")
            item.commentcls = CachedVariable("commentcls")
            item.midcolmargin = CachedVariable("midcolmargin")
            item.comment_label = CachedVariable("numcomments")

            item.as_deleted = False
            if item.deleted and not c.user_is_admin:
                item.author = DeletedUser()
                item.as_deleted = True

            item.trial_info = trials.get(item._fullname, None)

            item.approval_checkmark = None

            if item.can_ban:
                verdict = getattr(item, "verdict", None)
                if verdict in ('admin-approved', 'mod-approved'):
                    approver = None
                    if getattr(item, "ban_info", None):
                        approver = item.ban_info.get("unbanner", None)

                    if approver:
                        item.approval_checkmark = _("approved by %s") % approver
                    else:
                        item.approval_checkmark = _("approved by a moderator")

                if item.trial_info is not None:
                    item.reveal_trial_info = True
                    item.use_big_modbuttons = True

        if user_is_loggedin:
            incr_counts(wrapped)

        # Run this last
        Printable.add_props(user, wrapped)

    @property
    def subreddit_slow(self):
        from subreddit import Subreddit
        """return's a link's subreddit. in most case the subreddit is already
        on the wrapped link (as .subreddit), and that should be used
        when possible. """
        return Subreddit._byID(self.sr_id, True, return_dict = False)

# Note that there are no instances of PromotedLink or LinkCompressed,
# so overriding their methods here will not change their behaviour
# (except for add_props). These classes are used to override the
# render_class on a Wrapped to change the template used for rendering

class PromotedLink(Link):
    _nodb = True

    @classmethod
    def add_props(cls, user, wrapped):
        # prevents cyclic dependencies
        from r2.lib import promote
        Link.add_props(user, wrapped)
        user_is_sponsor = c.user_is_sponsor

        status_dict = dict((v, k) for k, v in promote.STATUS.iteritems())
        for item in wrapped:
            # these are potentially paid for placement
            item.nofollow = True
            item.user_is_sponsor = user_is_sponsor
            status = getattr(item, "promote_status", -1)
            if item.is_author or c.user_is_sponsor:
                item.rowstyle = "link " + promote.STATUS.name[status].lower()
            else:
                item.rowstyle = "link promoted"
        # Run this last
        Printable.add_props(user, wrapped)

class Comment(Thing, Printable):
    _data_int_props = Thing._data_int_props + ('reported',)
    _defaults = dict(reported = 0, parent_id = None, 
                     moderator_banned = False, new = False, 
                     banned_before_moderator = False)
    _essentials = ('link_id', 'author_id')

    def _markdown(self):
        pass

    def _delete(self):
        link = Link._byID(self.link_id, data = True)
        link._incr('num_comments', -1)

    @classmethod
    def _new(cls, author, link, parent, body, ip):
        c = Comment(_ups = 1,
                    body = body,
                    link_id = link._id,
                    sr_id = link.sr_id,
                    author_id = author._id,
                    ip = ip)

        c._spam = author._spam

        #these props aren't relations
        if parent:
            c.parent_id = parent._id

        link._incr('num_comments', 1)

        to = None
        name = 'inbox'
        if parent:
            to = Account._byID(parent.author_id)
        elif link.is_self:
            to = Account._byID(link.author_id)
            name = 'selfreply'

        c._commit()

        inbox_rel = None
        # only global admins can be message spammed.
        if to and (not c._spam or to.name in g.admins):
            inbox_rel = Inbox._add(to, c, name)

        return (c, inbox_rel)

    @property
    def subreddit_slow(self):
        from subreddit import Subreddit
        """return's a comments's subreddit. in most case the subreddit is already
        on the wrapped link (as .subreddit), and that should be used
        when possible. if sr_id does not exist, then use the parent link's"""
        self._safe_load()

        if hasattr(self, 'sr_id'):
            sr_id = self.sr_id
        else:
            l = Link._byID(self.link_id, True)
            sr_id = l.sr_id
        return Subreddit._byID(sr_id, True, return_dict = False)

    def keep_item(self, wrapped):
        return True

    cache_ignore = set(["subreddit", "link", "to"]
                       ).union(Printable.cache_ignore)
    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        s.extend([wrapped.body])
        return s

    def make_permalink(self, link, sr=None, context=None, anchor=False):
        url = link.make_permalink(sr) + self._id36
        if context:
            url += "?context=%d" % context
        if anchor:
            url += "#%s" % self._id36
        return url

    def make_permalink_slow(self, context=None, anchor=False):
        l = Link._byID(self.link_id, data=True)
        return self.make_permalink(l, l.subreddit_slow,
                                   context=context, anchor=anchor)

    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.template_helpers import add_attr
        from r2.lib import promote
        #fetch parent links
        links = Link._byID(set(l.link_id for l in wrapped), data = True,
                           return_dict = True)

        #get srs for comments that don't have them (old comments)
        for cm in wrapped:
            if not hasattr(cm, 'sr_id'):
                cm.sr_id = links[cm.link_id].sr_id

        subreddits = Subreddit._byID(set(cm.sr_id for cm in wrapped),
                                     data=True,return_dict=False)
        cids = dict((w._id, w) for w in wrapped)
        parent_ids = set(cm.parent_id for cm in wrapped
                         if getattr(cm, 'parent_id', None)
                         and cm.parent_id not in cids)
        parents = {}
        if parent_ids:
            parents = Comment._byID(parent_ids, data=True)

        can_reply_srs = set(s._id for s in subreddits if s.can_comment(user)) \
                        if c.user_is_loggedin else set()
        can_reply_srs.add(promote.get_promote_srid())

        min_score = user.pref_min_comment_score

        profilepage = c.profilepage
        user_is_admin = c.user_is_admin
        user_is_loggedin = c.user_is_loggedin
        focal_comment = c.focal_comment

        for item in wrapped:
            # for caching:
            item.profilepage = c.profilepage
            item.link = links.get(item.link_id)

            if (item.link._score <= 1 or item.score < 3 or
                item.link._spam or item._spam or item.author._spam):
                item.nofollow = True
            else:
                item.nofollow = False

            if not hasattr(item, 'subreddit'):
                item.subreddit = item.subreddit_slow
            if item.author_id == item.link.author_id and not item.link._deleted:
                add_attr(item.attribs, 'S',
                         link = item.link.make_permalink(item.subreddit))
            if not hasattr(item, 'target'):
                item.target = None
            if item.parent_id:
                if item.parent_id in cids:
                    item.parent_permalink = '#' + utils.to36(item.parent_id)
                else:
                    parent = parents[item.parent_id]
                    item.parent_permalink = parent.make_permalink(item.link, item.subreddit)
            else:
                item.parent_permalink = None

            item.can_reply = c.can_reply or (item.sr_id in can_reply_srs) 


            # not deleted on profile pages,
            # deleted if spam and not author or admin
            item.deleted = (not profilepage and
                           (item._deleted or
                            (item._spam and
                             item.author != user and
                             not item.show_spam)))

            extra_css = ''
            if item.deleted:
                extra_css += "grayed"
                if not user_is_admin:
                    item.author = DeletedUser()
                    item.body = '[deleted]'


            if focal_comment == item._id36:
                extra_css += " border"


            # don't collapse for admins, on profile pages, or if deleted
            item.collapsed = ((item.score < min_score) and
                             not (profilepage or
                                  item.deleted or
                                  user_is_admin))

            item.editted = getattr(item, "editted", False)


            #will get updated in builder
            item.num_children = 0
            item.score_fmt = Score.points
            item.permalink = item.make_permalink(item.link, item.subreddit)

            item.is_author = (user == item.author)
            item.is_focal  = (focal_comment == item._id36)

            #will seem less horrible when add_props is in pages.py
            from r2.lib.pages import UserText
            item.usertext = UserText(item, item.body,
                                     editable = item.is_author,
                                     nofollow = item.nofollow,
                                     target = item.target,
                                     extra_css = extra_css)
        # Run this last
        Printable.add_props(user, wrapped)

class StarkComment(Comment):
    """Render class for the comments in the top-comments display in
       the reddit toolbar"""
    _nodb = True

class MoreMessages(Printable):
    cachable = False
    display = ""
    new = False
    was_comment = False
    is_collapsed = True

    def __init__(self, parent, child):
        self.parent = parent
        self.child = child

    @staticmethod
    def wrapped_cache_key(item, style):
        return False

    @property
    def _fullname(self):
        return self.parent._fullname

    @property
    def _id36(self):
        return self.parent._id36

    @property
    def subject(self):
        return self.parent.subject

    @property
    def childlisting(self):
        return self.child

    @property
    def to(self):
        return self.parent.to

    @property
    def author(self):
        return self.parent.author

    @property
    def recipient(self):
        return self.parent.recipient

    @property
    def sr_id(self):
        return self.parent.sr_id

    @property
    def subreddit(self):
        return self.parent.subreddit


class MoreComments(Printable):
    cachable = False
    display = ""
    
    @staticmethod
    def wrapped_cache_key(item, style):
        return False
    
    def __init__(self, link, depth, parent=None):
        if parent:
            self.parent_id = parent._id
            self.parent_name = parent._fullname
            self.parent_permalink = parent.make_permalink(link, 
                                                          link.subreddit_slow)
        self.link_name = link._fullname
        self.link_id = link._id
        self.depth = depth
        self.children = []
        self.count = 0

    @property
    def _fullname(self):
        return self.children[0]._fullname if self.children else 't0_blah'

    @property
    def _id36(self):
        return self.children[0]._id36 if self.children else 't0_blah'


class MoreRecursion(MoreComments):
    pass

class MoreChildren(MoreComments):
    pass

class Message(Thing, Printable):
    _defaults = dict(reported = 0, was_comment = False, parent_id = None,
                     new = False,  first_message = None, to_id = None,
                     sr_id = None, to_collapse = None, author_collapse = None)
    _data_int_props = Thing._data_int_props + ('reported', )
    cache_ignore = set(["to", "subreddit"]).union(Printable.cache_ignore)

    @classmethod
    def _new(cls, author, to, subject, body, ip, parent = None, sr = None):
        m = Message(subject = subject,
                    body = body,
                    author_id = author._id,
                    new = True, 
                    ip = ip)
        m._spam = author._spam
        sr_id = None
        # check to see if the recipient is a subreddit and swap args accordingly
        if to and isinstance(to, Subreddit):
            to_subreddit = True
            to, sr = None, to
        else:
            to_subreddit = False

        if sr:
            sr_id = sr._id
        if parent:
            m.parent_id = parent._id
            if parent.first_message:
                m.first_message = parent.first_message
            else:
                m.first_message = parent._id
            if parent.sr_id:
                sr_id = parent.sr_id

        if not to and not sr_id:
            raise CreationError, "Message created with neither to nor sr_id"

        m.to_id = to._id if to else None
        if sr_id is not None:
            m.sr_id = sr_id

        m._commit()

        inbox_rel = None
        if sr_id and not sr:
            sr = Subreddit._byID(sr_id)

        inbox_rel = []
        if sr_id:
            # if there is a subreddit id, and it's either a reply or
            # an initial message to an SR, add to the moderator inbox
            # (i.e., don't do it for automated messages from the SR)
            if parent or to_subreddit:
                inbox_rel.append(ModeratorInbox._add(sr, m, 'inbox'))
            if author.name in g.admins:
                m.distinguished = 'admin'
                m._commit()
            elif sr.is_moderator(author):
                m.distinguished = 'yes'
                m._commit()
        # if there is a "to" we may have to create an inbox relation as well
        # also, only global admins can be message spammed.
        if to and (not m._spam or to.name in g.admins):
            # if the current "to" is not a sr moderator,
            # they need to be notified
            if not sr_id or not sr.is_moderator(to):
                inbox_rel.append(Inbox._add(to, m, 'inbox'))
            # find the message originator
            elif sr_id and m.first_message:
                first = Message._byID(m.first_message, True)
                orig = Account._byID(first.author_id)
                # if the originator is not a moderator...
                if not sr.is_moderator(orig) and orig._id != author._id:
                    inbox_rel.append(Inbox._add(orig, m, 'inbox'))
        return (m, inbox_rel)

    @property
    def permalink(self):
        return "/message/messages/%s" % self._id36

    def can_view_slow(self):
        if c.user_is_loggedin:
            # simple case from before:
            if (c.user_is_admin or
                c.user._id in (self.author_id, self.to_id)):
                return True
            elif self.sr_id:
                sr = Subreddit._byID(self.sr_id)
                is_moderator = sr.is_moderator(c.user)
                # moderators can view messages on subreddits they moderate
                if is_moderator:
                    return True
                elif self.first_message: 
                    first = Message._byID(self.first_message, True)
                    return (first.author_id == c.user._id)


    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.db import queries
        #TODO global-ish functions that shouldn't be here?
        #reset msgtime after this request
        msgtime = c.have_messages

        # make sure there is a sr_id set:
        for w in wrapped:
            if not hasattr(w, "sr_id"):
                w.sr_id = None

        # load the to fields if one exists
        to_ids = set(w.to_id for w in wrapped if w.to_id is not None)
        tos = Account._byID(to_ids, True) if to_ids else {}

        # load the subreddit field if one exists:
        sr_ids = set(w.sr_id for w in wrapped if w.sr_id is not None)
        m_subreddits = Subreddit._byID(sr_ids, data = True, return_dict = True)

        # load the links and their subreddits (if comment-as-message)
        links = Link._byID(set(l.link_id for l in wrapped if l.was_comment),
                           data = True,
                           return_dict = True)
        # subreddits of the links (for comment-as-message)
        l_subreddits = Subreddit._byID(set(l.sr_id for l in links.values()),
                                       data = True, return_dict = True)

        parents = Comment._byID(set(l.parent_id for l in wrapped
                                  if l.parent_id and l.was_comment),
                                data = True, return_dict = True)

        # load the inbox relations for the messages to determine new-ness
        # TODO: query cache?
        inbox = Inbox._fast_query(c.user,
                                  [item.lookups[0] for item in wrapped],
                                  ['inbox', 'selfreply'])

        # we don't care about the username or the rel name
        inbox = dict((m._fullname, v)
                     for (u, m, n), v in inbox.iteritems() if v)

        msgs = filter (lambda x: isinstance(x.lookups[0], Message), wrapped)

        modinbox = ModeratorInbox._fast_query(m_subreddits.values(),
                                              msgs,
                                              ['inbox'] )

        # best to not have to eager_load the things
        def make_message_fullname(mid):
            return "t%s_%s" % (utils.to36(Message._type_id), utils.to36(mid))
        modinbox = dict((make_message_fullname(v._thing2_id), v)
                     for (u, m, n), v in modinbox.iteritems() if v)

        for item in wrapped:
            item.to = tos.get(item.to_id)
            if item.sr_id:
                item.recipient = (item.author_id != c.user._id)
            else:
                item.recipient = (item.to_id == c.user._id)

            # new-ness is stored on the relation
            if item.author_id == c.user._id:
                item.new = False
            elif item._fullname in inbox:
                item.new = getattr(inbox[item._fullname], "new", False)
                # wipe new messages if preferences say so, and this isn't a feed
                # and it is in the user's personal inbox
                if (item.new and c.user.pref_mark_messages_read
                    and c.extension not in ("rss", "xml", "api", "json")):
                    queries.set_unread(inbox[item._fullname]._thing2,
                                       c.user, False)
            elif item._fullname in modinbox:
                item.new = getattr(modinbox[item._fullname], "new", False)
            else:
                item.new = False


            item.score_fmt = Score.none

            item.message_style = ""
            # comment as message:
            if item.was_comment:
                link = links[item.link_id]
                sr = l_subreddits[link.sr_id]
                item.to_collapse = False
                item.author_collapse = False
                item.link_title = link.title
                item.link_permalink = link.make_permalink(sr)
                if item.parent_id:
                    item.subject = _('comment reply')
                    item.message_style = "comment-reply"
                    parent = parents[item.parent_id]
                    item.parent = parent._fullname
                    item.parent_permalink = parent.make_permalink(link, sr)
                else:
                    item.subject = _('post reply')
                    item.message_style = "post-reply"
            elif item.sr_id is not None:
                item.subreddit = m_subreddits[item.sr_id]

            if c.user.pref_no_profanity:
                item.subject = profanity_filter(item.subject)

            item.is_collapsed = None
            if not item.new:
                if item.recipient:
                    item.is_collapsed = item.to_collapse
                if item.author_id == c.user._id:
                    item.is_collapsed = item.author_collapse
                if c.user.pref_collapse_read_messages:
                    item.is_collapsed = (item.is_collapsed is not False)

        # Run this last
        Printable.add_props(user, wrapped)

    @property
    def subreddit_slow(self):
        from subreddit import Subreddit
        if self.sr_id:
            return Subreddit._byID(self.sr_id)

    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        s.extend([wrapped.new, wrapped.collapsed])
        return s

    def keep_item(self, wrapped):
        return True

class SaveHide(Relation(Account, Link)): pass
class Click(Relation(Account, Link)): pass

class Inbox(MultiRelation('inbox',
                          Relation(Account, Comment),
                          Relation(Account, Message))):

    _defaults = dict(new = False)

    @classmethod
    def _add(cls, to, obj, *a, **kw):
        i = Inbox(to, obj, *a, **kw)
        i.new = True
        i._commit()

        if not to._loaded:
            to._load()

        #if there is not msgtime, or it's false, set it
        if not hasattr(to, 'msgtime') or not to.msgtime:
            to.msgtime = obj._date
            to._commit()

        return i

    @classmethod
    def set_unread(cls, thing, unread, to = None):
        inbox_rel = cls.rel(Account, thing.__class__)
        if to:
            inbox = inbox_rel._query(inbox_rel.c._thing2_id == thing._id,
                                     eager_load = True)
        else:
            inbox = inbox_rel._query(inbox_rel.c._thing2_id == thing._id,
                                     inbox_rel.c._thing1_id == to._id,
                                     eager_load = True)
        res = []
        for i in inbox:
            if i:
                i.new = unread
                i._commit()
                res.append(i)
        return res

class LinkOnTrial(Printable):
    @classmethod
    def add_props(cls, user, wrapped):
        Link.add_props(user, wrapped)
        for item in wrapped:
            item.rowstyle = "link ontrial"
        # Run this last
        Printable.add_props(user, wrapped)

class ModeratorInbox(Relation(Subreddit, Message)):
    #TODO: shouldn't dupe this
    @classmethod
    def _add(cls, sr, obj, *a, **kw):
        i = ModeratorInbox(sr, obj, *a, **kw)
        i.new = True
        i._commit()

        if not sr._loaded:
            sr._load()

        moderators = Account._byID(sr.moderator_ids(), return_dict = False)
        for m in moderators:
            if obj.author_id != m._id and not getattr(m, 'modmsgtime', None):
                m.modmsgtime = obj._date
                m._commit()

        return i

    @classmethod
    def set_unread(cls, thing, unread):
        inbox = cls._query(cls.c._thing2_id == thing._id,
                           eager_load = True)
        res = []
        for i in inbox:
            if i:
                i.new = unread
                i._commit()
                res.append(i)
        return res
