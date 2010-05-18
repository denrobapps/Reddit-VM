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
from r2.lib.wrapped import Wrapped, Templated, CachedTemplate
from r2.models import Account, Default, make_feedurl
from r2.models import FakeSubreddit, Subreddit, Ad, AdSR
from r2.models import Friends, All, Sub, NotFound, DomainSR, Random, Mod, RandomNSFW
from r2.models import Link, Printable, Trophy, bidding, PromotionWeights
from r2.config import cache
from r2.lib.tracking import AdframeInfo
from r2.lib.jsonresponse import json_respond
from r2.lib.jsontemplates import is_api
from pylons.i18n import _, ungettext
from pylons import c, request, g
from pylons.controllers.util import abort

from r2.lib import promote
from r2.lib.traffic import load_traffic, load_summary
from r2.lib.captcha import get_iden
from r2.lib.contrib.markdown import markdown
from r2.lib.filters import spaceCompress, _force_unicode, _force_utf8
from r2.lib.filters import unsafe, websafe, SC_ON, SC_OFF
from r2.lib.menus import NavButton, NamedButton, NavMenu, PageNameNav, JsButton
from r2.lib.menus import SubredditButton, SubredditMenu, ModeratorMailButton
from r2.lib.menus import OffsiteButton, menu, JsNavMenu
from r2.lib.strings import plurals, rand_strings, strings, Score
from r2.lib.utils import title_to_url, query_string, UrlParser, to_js, vote_hash
from r2.lib.utils import link_duplicates, make_offset_date, to_csv, median
from r2.lib.utils import trunc_time
from r2.lib.template_helpers import add_sr, get_domain
from r2.lib.subreddit_search import popular_searches
from r2.lib.scraper import get_media_embed
from r2.lib.log import log_text
from r2.lib.memoize import memoize

import sys, random, datetime, locale, calendar, simplejson, re, time
import graph, pycountry
from itertools import chain
from urllib import quote

from things import wrap_links, default_thing_wrapper

datefmt = _force_utf8(_('%d %b %Y'))

def get_captcha():
    if not c.user_is_loggedin or c.user.needs_captcha():
        return get_iden()

def responsive(res, space_compress = False):
    """
    Use in places where the template is returned as the result of the
    controller so that it becomes compatible with the page cache.
    """
    if is_api():
        res = json_respond(res)
    elif space_compress:
        res = spaceCompress(res)
    c.response.content = res
    return c.response

class Reddit(Templated):
    '''Base class for rendering a page on reddit.  Handles toolbar creation,
    content of the footers, and content of the corner buttons.

    Constructor arguments:

        space_compress -- run r2.lib.filters.spaceCompress on render
        loginbox -- enable/disable rendering of the small login box in the right margin
          (only if no user is logged in; login box will be disabled for a logged in user)
        show_sidebar -- enable/disable content in the right margin
        
        infotext -- text to display in a <p class="infotext"> above the content
        nav_menus -- list of Menu objects to be shown in the area below the header
        content -- renderable object to fill the main content well in the page.

    settings determined at class-declaration time

        create_reddit_box  -- enable/disable display of the "Creat a reddit" box
        submit_box         -- enable/disable display of the "Submit" box
        searcbox           -- enable/disable display of the "search" box in the header
        extension_handling -- enable/disable rendering using non-html templates
          (e.g. js, xml for rss, etc.)
    '''

    create_reddit_box  = True
    submit_box         = True
    footer             = True
    searchbox          = True
    extension_handling = True
    enable_login_cover = True
    site_tracking      = True
    show_firsttext     = True
    additional_css     = None

    def __init__(self, space_compress = True, nav_menus = None, loginbox = True,
                 infotext = '', content = None, title = '', robots = None, 
                 show_sidebar = True, footer = True, **context):
        Templated.__init__(self, **context)
        self.title          = title
        self.robots         = robots
        self.infotext       = infotext
        self.loginbox       = True
        self.show_sidebar   = show_sidebar
        self.space_compress = space_compress
        # instantiate a footer
        self.footer         = RedditFooter() if footer else None
        
        #put the sort menus at the top
        self.nav_menu = MenuArea(menus = nav_menus) if nav_menus else None

        #add the infobar
        self.infobar = None
        if self.show_firsttext and not infotext:
            if g.read_only_mode:
                infotext = strings.read_only_msg
            elif c.firsttime == 'iphone':
                infotext = strings.iphone_first
            elif c.firsttime and c.site.firsttext:
                infotext = c.site.firsttext
        if infotext:
            self.infobar = InfoBar(message = infotext)

        self.srtopbar = None
        if not c.cname:
            self.srtopbar = SubredditTopBar()

        if c.user_is_loggedin and self.show_sidebar and not is_api():
            self._content = PaneStack([ShareLink(), content])
        else:
            self._content = content

        self.toolbars = self.build_toolbars()

    def sr_admin_menu(self):
        buttons = [NamedButton('edit', css_class = 'reddit-edit'),
                   NamedButton('modmail', dest = "message/inbox",
                               css_class = 'moderator-mail'),
                   NamedButton('moderators', css_class = 'reddit-moderators')]

        if c.site.type != 'public':
            buttons.append(NamedButton('contributors',
                                       css_class = 'reddit-contributors'))
        elif (c.user_is_loggedin and
              (c.site.is_moderator(c.user) or c.user_is_admin)):
            buttons.append(NavButton(menu.contributors, "contributors",
                                     css_class = 'reddit-contributors'))

        buttons.extend([
                NamedButton('traffic', css_class = 'reddit-traffic'),
                NamedButton('reports', css_class = 'reddit-reported'),
                NamedButton('spam', css_class = 'reddit-spam'),
                NamedButton('banned', css_class = 'reddit-ban'),
                ])
        return [NavMenu(buttons, type = "flat_vert", base_path = "/about/",
                        css_class = "icon-menu",  separator = '')]

    def sr_moderators(self, limit = 10):
        accounts = Account._byID([uid
                                  for uid in c.site.moderators[:limit]],
                                 data=True, return_dict=False)
        return [WrappedUser(a) for a in accounts if not a._deleted]

    def rightbox(self):
        """generates content in <div class="rightbox">"""

        ps = PaneStack(css_class='spacer')

        if self.searchbox:
            ps.append(SearchForm())

        if not c.user_is_loggedin and self.loginbox and not g.read_only_mode:
            ps.append(LoginFormWide())

        if not isinstance(c.site, FakeSubreddit):
            ps.append(SponsorshipBox())

        no_ads_yet = True
        #don't show the subreddit info bar on cnames
        if not isinstance(c.site, FakeSubreddit) and not c.cname:
            ps.append(SubredditInfoBar())
            ps.append(Ads())
            no_ads_yet = False

        if self.submit_box:
            ps.append(SideBox(_('Submit a link'),
                              '/submit', 'submit',
                              sr_path = True,
                              subtitles = [strings.submit_box_text],
                              show_cover = True))

        if self.create_reddit_box:
           ps.append(SideBox(_('Create your own reddit'),
                              '/reddits/create', 'create',
                              subtitles = rand_strings.get("create_reddit", 2),
                              show_cover = True, nocname=True))

        if not isinstance(c.site, FakeSubreddit) and not c.cname:
            moderators = self.sr_moderators()
            if moderators:
                total = len(c.site.moderators)
                more_text = mod_href = ""
                if total > len(moderators):
                    more_text = "...and %d more" % (total - len(moderators))
                    mod_href = "http://%s/about/moderators" % get_domain()
                helplink = ("/message/compose?to=%%23%s" % c.site.name,
                            "message the moderators")
                ps.append(SideContentBox(_('moderators'), moderators,
                                         helplink = helplink, 
                                         more_href = mod_href,
                                         more_text = more_text))

            if (c.user_is_loggedin and
                (c.site.is_moderator(c.user) or c.user_is_admin)):
                ps.append(SideContentBox(_('admin box'), self.sr_admin_menu()))


        if no_ads_yet:
            ps.append(Ads())

        if c.user_is_admin:
            ps.append(Admin_Rightbox())

        if c.user.pref_clickgadget and c.recent_clicks:
            ps.append(SideContentBox(_("Recently viewed links"),
                                     [ClickGadget(c.recent_clicks)]))

        return ps

    def render(self, *a, **kw):
        """Overrides default Templated.render with two additions
           * support for rendering API requests with proper wrapping
           * support for space compression of the result
        In adition, unlike Templated.render, the result is in the form of a pylons
        Response object with it's content set.
        """
        res = Templated.render(self, *a, **kw)
        return responsive(res, self.space_compress)
    
    def corner_buttons(self):
        """set up for buttons in upper right corner of main page."""
        buttons = []
        if c.user_is_loggedin:
            if c.user.name in g.admins:
                if c.user_is_admin:
                   buttons += [NamedButton("adminoff", False,
                                           nocname=not c.authorized_cname,
                                           target = "_self")]
                else:
                   buttons += [NamedButton("adminon",  False,
                                           nocname=not c.authorized_cname,
                                           target = "_self")]
            buttons += [NamedButton("prefs", False,
                                  css_class = "pref-lang")]
        else:
            lang = c.lang.split('-')[0] if c.lang else ''
            buttons += [JsButton(g.lang_name.get(lang, lang),  
                                  onclick = "return showlang();",
                                  css_class = "pref-lang")]
        return NavMenu(buttons, base_path = "/", type = "flatlist")

    def build_toolbars(self):
        """Sets the layout of the navigation topbar on a Reddit.  The result
        is a list of menus which will be rendered in order and
        displayed at the top of the Reddit."""
        if c.site == Friends:
            main_buttons = [NamedButton('new', dest='', aliases=['/hot']),
                            NamedButton('comments')]
        else:
            main_buttons = [NamedButton('hot', dest='', aliases=['/hot']),
                            NamedButton('new'), 
                            NamedButton('controversial'),
                            NamedButton('top'),
                            NamedButton('saved', False)
                            ]

        more_buttons = []

        if c.user_is_loggedin:
            if c.user_is_admin:
                more_buttons.append(NamedButton('admin', False))
                more_buttons.append(NamedButton('traffic', False))
            if c.user.pref_show_promote or c.user_is_sponsor:
                more_buttons.append(NavButton(menu.promote, 'promoted', False))

        #if there's only one button in the dropdown, get rid of the dropdown
        if len(more_buttons) == 1:
            main_buttons.append(more_buttons[0])
            more_buttons = []

        toolbar = [NavMenu(main_buttons, type='tabmenu')]
        if more_buttons:
            toolbar.append(NavMenu(more_buttons, title=menu.more, type='tabdrop'))

        if c.site != Default and not c.cname:
            toolbar.insert(0, PageNameNav('subreddit'))

        return toolbar

    def __repr__(self):
        return "<Reddit>"

    @staticmethod
    def content_stack(panes, css_class = None):
        """Helper method for reordering the content stack."""
        return PaneStack(filter(None, panes), css_class = css_class)

    def content(self):
        """returns a Wrapped (or renderable) item for the main content div."""
        return self.content_stack((self.infobar, self.nav_menu, self._content))

class RedditHeader(Templated):
    def __init__(self):
        pass

class RedditFooter(CachedTemplate):
    def cachable_attrs(self):
        return [('path', request.path),
                ('buttons', [[(x.title, x.path) for x in y] for y in self.nav])]

    def __init__(self):
        self.nav = [NavMenu([NamedButton("mobile", False, nocname=True),
                         OffsiteButton("rss", dest = '/.rss'),
                         NamedButton("store", False, nocname=True),
                         NamedButton("awards", False, nocname=True),
                         NamedButton('random', False, nocname=False),
                         ],
                        title = _('site links'), type = 'flat_vert',
                        separator = ''),

                    NavMenu([NamedButton("help", False, nocname=True),
                         OffsiteButton(_("FAQ"), dest = '/help/faq',
                                       nocname=True),
                         OffsiteButton(_("reddiquette"), nocname=True,
                                       dest = '/help/reddiquette'),
                         NamedButton("feedback", False),
                         NamedButton("i18n", False),
                             ],
                        title = _('help'), type = 'flat_vert',
                        separator = ''),
                    NavMenu([NamedButton("bookmarklets", False),
                         NamedButton("buttons", True),
                         NamedButton("code", False, nocname=True),
                         NamedButton("socialite", False),
                         NamedButton("widget", True),
                         NamedButton("iphone", False),],
                        title = _('reddit tools'), type = 'flat_vert',
                        separator = ''),
                    NavMenu([NamedButton("blog", False, nocname=True),
                         NamedButton("promote", False, nocname=True,
                                     dest = '/promoted', css_class = "red"),
                         NamedButton("ad_inq", False, nocname=True),
                         OffsiteButton('reddit.tv', "http://www.reddit.tv"),
                         OffsiteButton('redditall', "http://www.redditall.com"),
                         OffsiteButton(_('job board'),
                                       "http://www.redditjobs.com")],
                        title = _('about us'), type = 'flat_vert',
                        separator = ''),
                    NavMenu([OffsiteButton('BaconBuzz',
                                       "http://www.baconbuzz.com"),
                         OffsiteButton('Destructoid reddit',
                                       "http://reddit.destructoid.com"),
                         OffsiteButton('TheCuteList',
                                       "http://www.thecutelist.com"),
                         OffsiteButton('The Independent reddit',
                                       "http://reddit.independent.co.uk"),
                         OffsiteButton('redditGadgetGuide',
                                       "http://www.redditgadgetguide.com"),
                         OffsiteButton('WeHeartGossip',
                                       "http://www.weheartgossip.com"),
                         OffsiteButton('idealistNews',
                                       "http://www.idealistnews.com"),],
                        title = _('brothers'), type = 'flat_vert',
                        separator = ''),
                    NavMenu([OffsiteButton('Wired.com',
                                       "http://www.wired.com"),
                         OffsiteButton('Ars Technica',
                                       "http://www.arstechnica.com"),
                         OffsiteButton('Style.com',
                                       "http://www.style.com"),
                         OffsiteButton('Epicurious.com',
                                       "http://www.epicurious.com"),
                         OffsiteButton('Concierge.com',
                                       "http://www.concierge.com")],
                        title = _('sisters'), type = 'flat_vert',
                        separator = '')
                    ]
        CachedTemplate.__init__(self)

class ClickGadget(Templated):
    def __init__(self, links, *a, **kw):
        self.links = links
        self.content = ''
        if c.user_is_loggedin and self.links:
            self.content = self.make_content()
        Templated.__init__(self, *a, **kw)

    def make_content(self):
        #this will disable the hardcoded widget styles
        request.get.style = "off"
        wrapper = default_thing_wrapper(embed_voting_style = 'votable',
                                        style = "htmllite")
        content = wrap_links(self.links, wrapper = wrapper)

        return content.render(style = "htmllite")


class RedditMin(Reddit):
    """a version of Reddit that has no sidebar, toolbar, footer,
       etc"""
    footer       = False
    show_sidebar = False
    show_firsttext = False

class LoginFormWide(CachedTemplate):
    """generates a login form suitable for the 300px rightbox."""
    def __init__(self):
        self.cname = c.cname
        self.auth_cname = not c.frameless_cname or c.authorized_cname
        CachedTemplate.__init__(self)

class SubredditInfoBar(CachedTemplate):
    """When not on Default, renders a sidebox which gives info about
    the current reddit, including links to the moderator and
    contributor pages, as well as links to the banning page if the
    current user is a moderator."""

    def __init__(self, site = None):
        site = site or c.site

        #hackity hack. do i need to add all the others props?
        self.sr = list(wrap_links(site))[0]

        # we want to cache on the number of subscribers
        self.subscribers = self.sr._ups
        
        #so the menus cache properly
        self.path = request.path
        CachedTemplate.__init__(self)
    
    def nav(self):
        buttons = [NavButton(plurals.moderators, 'moderators')]
        if self.type != 'public':
            buttons.append(NavButton(getattr(plurals, "approved submitters"), 'contributors'))

        if self.is_moderator or self.is_admin:
            buttons.extend([
                    NamedButton('spam'),
                    NamedButton('reports'),
                    NavButton(menu.banusers, 'banned'),
                    NamedButton('traffic'),
                    NamedButton('edit'),
                    ])
        return [NavMenu(buttons, type = "flat_vert", base_path = "/about/",
                        separator = '')]

class SponsorshipBox(Templated):
    pass

class SideContentBox(Templated):
    def __init__(self, title, content, helplink=None, extra_class=None,
                 more_href = None, more_text = "more"):
        Templated.__init__(self, title=title, helplink = helplink,
                           content=content, extra_class=extra_class,
                           more_href = more_href,
                           more_text = more_text)

class SideBox(CachedTemplate):
    """
    Generic sidebox used to generate the 'submit' and 'create a reddit' boxes.
    """
    def __init__(self, title, link, css_class='', subtitles = [],
                 show_cover = False, nocname=False, sr_path = False):
        CachedTemplate.__init__(self, link = link, target = '_top',
                           title = title, css_class = css_class,
                           sr_path = sr_path, subtitles = subtitles,
                           show_cover = show_cover, nocname=nocname)


class PrefsPage(Reddit):
    """container for pages accessible via /prefs.  No extension handling."""

    extension_handling = False

    def __init__(self, show_sidebar = False, *a, **kw):
        Reddit.__init__(self, show_sidebar = show_sidebar,
                        title = "%s (%s)" %(_("preferences"),
                                            c.site.name.strip(' ')),
                        *a, **kw)

    def build_toolbars(self):
        buttons = [NavButton(menu.options, '')]

        if c.user.pref_private_feeds:
            buttons.append(NamedButton('feeds'))

        buttons.extend([NamedButton('friends'),
                        NamedButton('update')])
        #if CustomerID.get_id(user):
        #    buttons += [NamedButton('payment')]
        buttons += [NamedButton('delete')]
        return [PageNameNav('nomenu', title = _("preferences")), 
                NavMenu(buttons, base_path = "/prefs", type="tabmenu")]

class PrefOptions(Templated):
    """Preference form for updating language and display options"""
    def __init__(self, done = False):
        Templated.__init__(self, done = done)

class PrefFeeds(Templated):
    pass

class PrefUpdate(Templated):
    """Preference form for updating email address and passwords"""
    def __init__(self, email = True, password = True, verify = False):
        self.email = email
        self.password = password
        self.verify = verify
        Templated.__init__(self)

class PrefDelete(Templated):
    """preference form for deleting a user's own account."""
    pass


class MessagePage(Reddit):
    """Defines the content for /message/*"""
    def __init__(self, *a, **kw):
        if not kw.has_key('show_sidebar'):
            kw['show_sidebar'] = False
        Reddit.__init__(self, *a, **kw)
        if is_api():
            self.replybox = None
        else:
            self.replybox = UserText(item = None, creating = True,
                                     post_form = 'comment', display = False,
                                     cloneable = True)
            

    def content(self):
        return self.content_stack((self.replybox,
                                   self.infobar,
                                   self.nav_menu,
                                   self._content))

    def build_toolbars(self):
        buttons =  [NamedButton('compose', sr_path = False),
                    NamedButton('inbox', aliases = ["/message/comments",
                                                    "/message/uread",
                                                    "/message/messages",
                                                    "/message/selfreply"],
                                sr_path = False),
                    NamedButton('sent', sr_path = False)]
        if c.show_mod_mail:
            buttons.append(ModeratorMailButton(menu.modmail, "moderator",
                                               sr_path = False))
        if not c.default_sr:
            buttons.append(ModeratorMailButton(
                _("%(site)s mail") % {'site': c.site.name}, "moderator",
                aliases = ["/about/message/inbox",
                           "/about/message/unread"]))
        return [PageNameNav('nomenu', title = _("message")), 
                NavMenu(buttons, base_path = "/message", type="tabmenu")]

class MessageCompose(Templated):
    """Compose message form."""
    def __init__(self,to='', subject='', message='', success='', 
                 captcha = None):
        Templated.__init__(self, to = to, subject = subject,
                         message = message, success = success, 
                         captcha = captcha)

    
class BoringPage(Reddit):
    """parent class For rendering all sorts of uninteresting,
    sortless, navless form-centric pages.  The top navmenu is
    populated only with the text provided with pagename and the page
    title is 'reddit.com: pagename'"""
    
    extension_handling= False
    
    def __init__(self, pagename, **context):
        self.pagename = pagename
        name = c.site.name or g.default_sr
        Reddit.__init__(self, title = "%s: %s" % (name, pagename),
                        **context)

    def build_toolbars(self):
        return [PageNameNav('nomenu', title = self.pagename)]

class HelpPage(BoringPage):
    def build_toolbars(self):
        return [PageNameNav('help', title = self.pagename)]

class FormPage(BoringPage):
    create_reddit_box  = False
    submit_box         = False
    """intended for rendering forms with no rightbox needed or wanted"""
    def __init__(self, pagename, show_sidebar = False, *a, **kw):
        BoringPage.__init__(self, pagename,  show_sidebar = show_sidebar,
                            *a, **kw)
        

class LoginPage(BoringPage):
    enable_login_cover = False

    """a boring page which provides the Login/register form"""
    def __init__(self, **context):
        context['loginbox'] = False
        self.dest = context.get('dest', '')
        context['show_sidebar'] = False
        BoringPage.__init__(self,  _("login or register"), **context)

    def content(self):
        kw = {}
        for x in ('user_login', 'user_reg'):
            kw[x] = getattr(self, x) if hasattr(self, x) else ''
        return Login(dest = self.dest, **kw)

class Login(Templated):
    """The two-unit login and register form."""
    def __init__(self, user_reg = '', user_login = '', dest=''):
        Templated.__init__(self, user_reg = user_reg, user_login = user_login,
                           dest = dest, captcha = Captcha())
    
class SearchPage(BoringPage):
    """Search results page"""
    searchbox = False

    def __init__(self, pagename, prev_search, elapsed_time,
                 num_results, search_params = {}, *a, **kw):
        self.searchbar = SearchBar(prev_search = prev_search,
                                   elapsed_time = elapsed_time,
                                   num_results = num_results,
                                   search_params = search_params)
        BoringPage.__init__(self, pagename, robots='noindex', *a, **kw)

    def content(self):
        return self.content_stack((self.searchbar, self.infobar,
                                   self.nav_menu, self._content))

class TakedownPage(BoringPage):
    def __init__(self, link):
        BoringPage.__init__(self, getattr(link, "takedown_title", _("bummer")), 
                            content = TakedownPane(link))

    def render(self, *a, **kw):
        response = BoringPage.render(self, *a, **kw)
        return response


class TakedownPane(Templated):
    def __init__(self, link, *a, **kw):
        self.link = link
        self.explanation = getattr(self.link, "explanation", 
                                   _("this page is no longer available due to a copyright claim."))
        Templated.__init__(self, *a, **kw)

class CommentsPanel(Templated):
    """the side-panel on the reddit toolbar frame that shows the top
       comments of a link"""

    def __init__(self, link = None, listing = None, expanded = False, *a, **kw):
        self.link = link
        self.listing = listing
        self.expanded = expanded

        Templated.__init__(self, *a, **kw)

class LinkInfoPage(Reddit):
    """Renders the varied /info pages for a link.  The Link object is
    passed via the link argument and the content passed to this class
    will be rendered after a one-element listing consisting of that
    link object.

    In addition, the rendering is reordered so that any nav_menus
    passed to this class will also be rendered underneath the rendered
    Link.
    """
    
    create_reddit_box = False

    def __init__(self, link = None, comment = None,
                 link_title = '', subtitle = None, duplicates = None,
                 *a, **kw):

        expand_children = kw.get("expand_children", not bool(comment))

        wrapper = default_thing_wrapper(expand_children=expand_children)

        # link_listing will be the one-element listing at the top
        self.link_listing = wrap_links(link, wrapper = wrapper)

        # link is a wrapped Link object
        self.link = self.link_listing.things[0]

        link_title = ((self.link.title) if hasattr(self.link, 'title') else '')

        # defaults whether or not there is a comment
        params = {'title':_force_unicode(link_title), 'site' : c.site.name}
        title = strings.link_info_title % params

        # only modify the title if the comment/author are neither deleted nor spam
        if comment and not comment._deleted and not comment._spam:
            author = Account._byID(comment.author_id, data=True)

            if not author._deleted and not author._spam:
                params = {'author' : author.name, 'title' : _force_unicode(link_title)}
                title = strings.permalink_title % params

        self.subtitle = subtitle

        # if we're already looking at the 'duplicates' page, we can
        # avoid doing this lookup twice
        if duplicates is None:
            self.duplicates = link_duplicates(self.link)
        else:
            self.duplicates = duplicates

        Reddit.__init__(self, title = title, *a, **kw)

    def build_toolbars(self):
        base_path = "/%s/%s/" % (self.link._id36, title_to_url(self.link.title))
        base_path = _force_utf8(base_path)


        def info_button(name, **fmt_args):
            return NamedButton(name, dest = '/%s%s' % (name, base_path),
                               aliases = ['/%s/%s' % (name, self.link._id36)],
                               fmt_args = fmt_args)
        buttons = []
        if not getattr(self.link, "disable_comments", False):
            buttons.extend([info_button('comments'),
                            info_button('related')])

            if not self.link.is_self and self.duplicates:
                buttons.append(info_button('duplicates',
                                           num = len(self.duplicates)))
            if len(self.link.title) < 200 and g.spreadshirt_url:
                buttons += [info_button('shirt')]

        if c.user_is_admin:
            buttons += [info_button('details')]

        # should we show a traffic tab (promoted and author or sponsor)
        if (self.link.promoted is not None and
            (c.user_is_sponsor or
             (c.user_is_loggedin and c.user._id == self.link.author_id))):
            buttons += [info_button('traffic')]

        toolbar = [NavMenu(buttons, base_path = "", type="tabmenu")]

        if c.site != Default and not c.cname:
            toolbar.insert(0, PageNameNav('subreddit'))

        return toolbar
    
    def content(self):
        return self.content_stack((self.infobar, self.link_listing,
                                   PaneStack([PaneStack((self.nav_menu,
                                                         self._content))],
                                             title = self.subtitle,
                                             css_class = "commentarea")))

    def rightbox(self):
        rb = Reddit.rightbox(self)
        if not (self.link.promoted and not c.user_is_sponsor):
            rb.insert(1, LinkInfoBar(a = self.link))
        return rb

class CommentPane(Templated):
    def cache_key(self):
        return "_".join(map(str, ["commentpane", self.article._fullname,
                                  self.article.num_comments,
                                  self.sort, self.num, c.lang,
                                  self.can_reply, c.render_style]))

    def __init__(self, article, sort, comment, context, num, **kw):
        # keys: lang, num, can_reply, render_style
        # disable: admin

        from r2.models import CommentBuilder, NestedListing
        from r2.controllers.reddit_base import UnloggedUser

        self.sort = sort
        self.num = num
        self.article = article

        # don't cache on permalinks or contexts, and keep it to html
        try_cache = not comment and not context and (c.render_style == "html")
        self.can_reply = False
        if try_cache and c.user_is_loggedin:
            sr = article.subreddit_slow
            c.can_reply = self.can_reply = sr.can_comment(c.user)
            # don't cache if the current user can ban comments in the listing
            try_cache = not sr.can_ban(c.user)
            # don't cache for users with custom hide threshholds
            try_cache = (c.user.pref_min_comment_score ==
                         Account._defaults["pref_min_comment_score"])

        def renderer():
            builder = CommentBuilder(article, sort, comment, context, **kw)
            listing = NestedListing(builder, num = num,
                                    parent_name = article._fullname)
            return listing.listing()

        # generate the listing we would make for this user if caching is disabled.
        my_listing = renderer()

        # for now, disable the cache if the user happens to be an author of anything.
        if try_cache:
            for t in self.listing_iter(my_listing):
                if getattr(t, "is_author", False):
                    try_cache = False
                    break

        if try_cache:
            # try to fetch the comment tree from the caceh
            key = self.cache_key()
            self.rendered = g.cache.get(key)
            if not self.rendered:
                # spoof an unlogged in user
                user = c.user
                logged_in = c.user_is_loggedin
                try:
                    c.user = UnloggedUser([c.lang])
                    c.user_is_loggedin = False

                    # render as if not logged in (but possibly with reply buttons)
                    self.rendered = renderer().render()
                    g.cache.set(key, self.rendered, time = 30)

                finally:
                    # undo the spoofing
                    c.user = user
                    c.user_is_loggedin = logged_in

            # figure out what needs to be updated on the listing
            likes = []
            dislikes = []
            is_friend = set()
            for t in self.listing_iter(my_listing):
                if not hasattr(t, "likes"):
                    # this is for MoreComments and MoreRecursion
                    continue
                if getattr(t, "friend", False) and not t.author._deleted:
                    is_friend.add(t.author._fullname)
                if t.likes:
                    likes.append(t._fullname)
                if t.likes is False:
                    dislikes.append(t._fullname)
            self.rendered += ThingUpdater(likes = likes,
                                          dislikes = dislikes,
                                          is_friend = is_friend).render()
            g.log.debug("using comment page cache")
        else:
            self.rendered = my_listing.render()

    def listing_iter(self, l):
        for t in l:
            yield t
            for x in self.listing_iter(getattr(t, "child", [])):
                yield x

    def render(self, *a, **kw):
        return self.rendered

class ThingUpdater(Templated):
    pass


class LinkInfoBar(Templated):
    """Right box for providing info about a link."""
    def __init__(self, a = None):
        if a:
            a = Wrapped(a)
        Templated.__init__(self, a = a, datefmt = datefmt)

class EditReddit(Reddit):
    """Container for the about page for a reddit"""
    extension_handling= False

    def __init__(self, *a, **kw):
        is_moderator = c.user_is_loggedin and \
            c.site.is_moderator(c.user) or c.user_is_admin

        title = _('manage your reddit') if is_moderator else \
                _('about %(site)s') % dict(site=c.site.name)

        Reddit.__init__(self, title = title, *a, **kw)
    
    def build_toolbars(self):
        if not c.cname:
            return [PageNameNav('subreddit')]
        else:
            return []

class SubredditsPage(Reddit):
    """container for rendering a list of reddits.  The corner
    searchbox is hidden and its functionality subsumed by an in page
    SearchBar for searching over reddits.  As a result this class
    takes the same arguments as SearchBar, which it uses to construct
    self.searchbar"""
    searchbox    = False
    submit_box   = False
    def __init__(self, prev_search = '', num_results = 0, elapsed_time = 0,
                 title = '', loginbox = True, infotext = None,
                 search_params = {}, *a, **kw):
        Reddit.__init__(self, title = title, loginbox = loginbox, infotext = infotext,
                        *a, **kw)
        self.searchbar = SearchBar(prev_search = prev_search,
                                   elapsed_time = elapsed_time,
                                   num_results = num_results,
                                   header = _('search reddits'),
                                   search_params = {}
                                   )
        self.sr_infobar = InfoBar(message = strings.sr_subscribe)

    def build_toolbars(self):
        buttons =  [NavButton(menu.popular, ""),
                    NamedButton("new")]
        if c.user_is_admin:
            buttons.append(NamedButton("banned"))

        if c.user_is_loggedin:
            #add the aliases to "my reddits" stays highlighted
            buttons.append(NamedButton("mine",
                                       aliases=['/reddits/mine/subscriber',
                                                '/reddits/mine/contributor',
                                                '/reddits/mine/moderator']))

        return [PageNameNav('reddits'),
                NavMenu(buttons, base_path = '/reddits', type="tabmenu")]

    def content(self):
        return self.content_stack((self.searchbar, self.nav_menu,
                                   self.sr_infobar, self._content))

    def rightbox(self):
        ps = Reddit.rightbox(self)
        ps.append(SideContentBox(_("your front page reddits"), [SubscriptionBox()]))
        return ps

class MySubredditsPage(SubredditsPage):
    """Same functionality as SubredditsPage, without the search box."""
    
    def content(self):
        return self.content_stack((self.nav_menu, self.infobar, self._content))


def votes_visible(user):
    """Determines whether to show/hide a user's votes.  They are visible:
     * if the current user is the user in question
     * if the user has a preference showing votes
     * if the current user is an administrator
    """
    return ((c.user_is_loggedin and c.user.name == user.name) or
            user.pref_public_votes or
            c.user_is_admin)

class ProfilePage(Reddit):
    """Container for a user's profile page.  As such, the Account
    object of the user must be passed in as the first argument, along
    with the current sub-page (to determine the title to be rendered
    on the page)"""
    
    searchbox         = False
    create_reddit_box = False
    submit_box        = False

    def __init__(self, user, *a, **kw):
        self.user     = user
        Reddit.__init__(self, *a, **kw)

    def build_toolbars(self):
        path = "/user/%s/" % self.user.name
        main_buttons = [NavButton(menu.overview, '/', aliases = ['/overview']),
                   NamedButton('comments'),
                   NamedButton('submitted')]
        
        if votes_visible(self.user):
            main_buttons += [NamedButton('liked'),
                        NamedButton('disliked'),
                        NamedButton('hidden')]

            
        toolbar = [PageNameNav('nomenu', title = self.user.name),
                   NavMenu(main_buttons, base_path = path, type="tabmenu")]

        if c.user_is_admin:
            from admin_pages import AdminProfileMenu
            toolbar.append(AdminProfileMenu(path))
        return toolbar
    

    def rightbox(self):
        rb = Reddit.rightbox(self)

        tc = TrophyCase(self.user)
        helplink = ( "/help/awards", _("what's this?") )
        scb = SideContentBox(title=_("trophy case"),
                 helplink=helplink, content=[tc],
                 extra_class="trophy-area")

        rb.push(scb)
        if c.user_is_admin:
            from admin_pages import AdminSidebar
            rb.push(AdminSidebar(self.user))
        rb.push(ProfileBar(self.user))

        return rb

class TrophyCase(Templated):
    def __init__(self, user):
        self.user = user
        self.trophies = []
        self.invisible_trophies = []

        self.dupe_trophies = []

        award_ids_seen = []

        for trophy in Trophy.by_account(user):
            if trophy._thing2.awardtype == 'invisible':
                self.invisible_trophies.append(trophy)
            elif trophy._thing2_id in award_ids_seen:
                self.dupe_trophies.append(trophy)
            else:
                self.trophies.append(trophy)
                award_ids_seen.append(trophy._thing2_id)

        self.cup_info = user.cup_info()
        Templated.__init__(self)

class ProfileBar(Templated): 
    """Draws a right box for info about the user (karma, etc)"""
    def __init__(self, user):
        Templated.__init__(self, user = user)
        self.is_friend = None
        self.my_fullname = c.user_is_loggedin and c.user._fullname
        if c.user_is_loggedin:
            self.is_friend = self.user._id in c.user.friends

class MenuArea(Templated):
    """Draws the gray box at the top of a page for sort menus"""
    def __init__(self, menus = []):
        Templated.__init__(self, menus = menus)

class InfoBar(Templated):
    """Draws the yellow box at the top of a page for info"""
    def __init__(self, message = '', extra_class = ''):
        Templated.__init__(self, message = message, extra_class = extra_class)


class RedditError(BoringPage):
    site_tracking = False
    def __init__(self, title, message = None):
        if not message:
            message = title
        BoringPage.__init__(self, title, loginbox=False,
                            show_sidebar = False, 
                            content=ErrorPage(message))

class Reddit404(BoringPage):
    site_tracking = False
    def __init__(self):
        ch=random.choice(['a','b','c'])
        BoringPage.__init__(self, _("page not found"), loginbox=False,
                            show_sidebar = False, 
                            content=UnfoundPage(ch))
        
class UnfoundPage(Templated):
    """Wrapper for the 404 page"""
    def __init__(self, choice):
        Templated.__init__(self, choice = choice)
    
class ErrorPage(Templated):
    """Wrapper for an error message"""
    def __init__(self, message = _("you aren't allowed to do that.")):
        Templated.__init__(self, message = message)
    
class Profiling(Templated):
    """Debugging template for code profiling using built in python
    library (only used in middleware)"""
    def __init__(self, header = '', table = [], caller = [], callee = [],
                 path = '', sort_order = ''):
        Templated.__init__(self, header = header, table = table, caller = caller,
                           callee = callee, path = path)

class Over18(Templated):
    """The creepy 'over 18' check page for nsfw content."""
    pass

class SubredditTopBar(Templated):
    """The horizontal strip at the top of most pages for navigating
    user-created reddits."""
    def __init__(self):
        Templated.__init__(self)

        self.my_reddits = Subreddit.user_subreddits(c.user, ids = False)

        p_srs = Subreddit.default_subreddits(ids = False,
                                             limit = Subreddit.sr_limit)
        self.pop_reddits = [ sr for sr in p_srs if sr.name not in g.automatic_reddits ]


# This doesn't actually work.
#        self.reddits = c.recent_reddits
#        for sr in pop_reddits:
#            if sr not in c.recent_reddits:
#                self.reddits.append(sr)

    def my_reddits_dropdown(self):
        drop_down_buttons = []    
        for sr in sorted(self.my_reddits, key = lambda sr: sr.name.lower()):
            drop_down_buttons.append(SubredditButton(sr))
        drop_down_buttons.append(NamedButton('edit', sr_path = False,
                                             css_class = 'bottom-option',
                                             dest = '/reddits/'))
        return SubredditMenu(drop_down_buttons,
                             title = _('my reddits'),
                             type = 'srdrop')

    def subscribed_reddits(self):
        srs = [SubredditButton(sr) for sr in
                        sorted(self.my_reddits,
                               key = lambda sr: sr._downs,
                               reverse=True)
                        if sr.name not in g.automatic_reddits
                        ]
        return NavMenu(srs,
                       type='flatlist', separator = '-',
                       css_class = 'sr-bar')

    def popular_reddits(self, exclude=[]):
        exclusions = set(exclude)
        buttons = [SubredditButton(sr)
                   for sr in self.pop_reddits if sr not in exclusions]

        return NavMenu(buttons,
                       type='flatlist', separator = '-',
                       css_class = 'sr-bar', _id = 'sr-bar')

    def special_reddits(self):
        reddits = [All, Random]
        if getattr(c.site, "over_18", False):
            reddits.append(RandomNSFW)
        if c.user_is_loggedin:
            if c.user.friends:
                reddits.append(Friends)
            if c.show_mod_mail:
                reddits.append(Mod)
        return NavMenu([SubredditButton(sr) for sr in reddits],
                       type = 'flatlist', separator = '-',
                       css_class = 'sr-bar')
    
    def sr_bar (self):
        sep = '<span class="separator">&nbsp;|&nbsp;</span>'
        menus = []
        menus.append(self.special_reddits())
        menus.append(RawString(sep))


        if not c.user_is_loggedin:
            menus.append(self.popular_reddits())
        else:
            if len(self.my_reddits) > g.sr_dropdown_threshold:
                menus = [self.my_reddits_dropdown()] + menus

            menus.append(self.subscribed_reddits())
            sep = '<span class="separator">&nbsp;&ndash;&nbsp;</span>'
            menus.append(RawString(sep))

            menus.append(self.popular_reddits(exclude=self.my_reddits))

        return menus

class SubscriptionBox(Templated):
    """The list of reddits a user is currently subscribed to to go in
    the right pane."""
    def __init__(self):
        srs = Subreddit.user_subreddits(c.user, ids = False)
        srs.sort(key = lambda sr: sr.name.lower())
        self.reddits = wrap_links(srs)
        Templated.__init__(self)

class CreateSubreddit(Templated):
    """reddit creation form."""
    def __init__(self, site = None, name = ''):
        Templated.__init__(self, site = site, name = name)

class SubredditStylesheet(Templated):
    """form for editing or creating subreddit stylesheets"""
    def __init__(self, site = None,
                 stylesheet_contents = ''):
        Templated.__init__(self, site = site,
                         stylesheet_contents = stylesheet_contents)

class CssError(Templated):
    """Rendered error returned to the stylesheet editing page via ajax"""
    def __init__(self, error):
        # error is an instance of cssutils.py:ValidationError
        Templated.__init__(self, error = error)

class UploadedImage(Templated):
    "The page rendered in the iframe during an upload of a header image"
    def __init__(self,status,img_src, name="", errors = {}, form_id = ""):
        self.errors = list(errors.iteritems())
        Templated.__init__(self, status=status, img_src=img_src, name = name,
                           form_id = form_id)

class Password(Templated):
    """Form encountered when 'recover password' is clicked in the LoginFormWide."""
    def __init__(self, success=False):
        Templated.__init__(self, success = success)

class PasswordReset(Templated):
    """Template for generating an email to the user who wishes to
    reset their password (step 2 of password recovery, after they have
    entered their user name in Password.)"""
    pass

class VerifyEmail(Templated):
    pass

class Promo_Email(Templated):
    pass

class ResetPassword(Templated):
    """Form for actually resetting a lost password, after the user has
    clicked on the link provided to them in the Password_Reset email
    (step 3 of password recovery.)"""
    pass


class Captcha(Templated):
    """Container for rendering robot detection device."""
    def __init__(self, error=None):
        self.error = _('try entering those letters again') if error else ""
        self.iden = get_captcha()
        Templated.__init__(self)

class PermalinkMessage(Templated):
    """renders the box on comment pages that state 'you are viewing a
    single comment's thread'"""
    def __init__(self, comments_url):
        Templated.__init__(self, comments_url = comments_url)

class PaneStack(Templated):
    """Utility class for storing and rendering a list of block elements."""
    
    def __init__(self, panes=[], div_id = None, css_class=None, div=False,
                 title=""):
        div = div or div_id or css_class or False
        self.div_id    = div_id
        self.css_class = css_class
        self.div       = div
        self.stack     = list(panes)
        self.title = title
        Templated.__init__(self)

    def append(self, item):
        """Appends an element to the end of the current stack"""
        self.stack.append(item)
    
    def push(self, item):
        """Prepends an element to the top of the current stack"""
        self.stack.insert(0, item)

    def insert(self, *a):
        """inerface to list.insert on the current stack"""
        return self.stack.insert(*a)


class SearchForm(Templated):
    """The simple search form in the header of the page.  prev_search
    is the previous search."""
    def __init__(self, prev_search = '', search_params = {}):
        Templated.__init__(self, prev_search = prev_search,
                           search_params = search_params)


class SearchBar(Templated):
    """More detailed search box for /search and /reddits pages.
    Displays the previous search as well as info of the elapsed_time
    and num_results if any."""
    def __init__(self, num_results = 0, prev_search = '', elapsed_time = 0,
                 search_params = {}, **kw):

        # not listed explicitly in args to ensure it translates properly
        self.header = kw.get('header', _("previous search"))

        self.prev_search  = prev_search
        self.elapsed_time = elapsed_time

        # All results are approximate unless there are fewer than 10.
        if num_results > 10:
            self.num_results = (num_results / 10) * 10
        else:
            self.num_results = num_results

        Templated.__init__(self, search_params = search_params)

class SearchFail(Templated):
    """Search failure page."""
    def __init__(self, **kw):
        md = SC_OFF + markdown(strings.search_failed % dict(
            link="javascript:tryagain\(\)")) + SC_ON

        self.errmsg = md

        Templated.__init__(self)


class Frame(Templated):
    """Frameset for the FrameToolbar used when a user hits /tb/. The
    top 30px of the page are dedicated to the toolbar, while the rest
    of the page will show the results of following the link."""
    def __init__(self, url='', title='', fullname=None):
        if title:
            title = (_('%(site_title)s via %(domain)s')
                     % dict(site_title = _force_unicode(title),
                            domain     = g.domain))
        else:
            title = g.domain
        Templated.__init__(self, url = url, title = title, fullname = fullname)

dorks_re = re.compile(r"https?://?([-\w.]*\.)?digg\.com/\w+\.\w+(/|$)")
class FrameToolbar(Wrapped):
    """The reddit voting toolbar used together with Frame."""

    cachable = True
    extension_handling = False
    cache_ignore = Link.cache_ignore
    def __init__(self, link, title = None, url = None, expanded = False, **kw):
        if link:
            self.title = link.title
            self.url = link.url
        else:
            self.title = title
            self.url = url

        self.expanded = expanded
        self.user_is_loggedin = c.user_is_loggedin
        self.have_messages = c.have_messages
        self.user_name = c.user.name if self.user_is_loggedin else ""
        self.cname = c.cname
        self.site_name = c.site.name
        self.site_description = c.site.description
        self.default_sr = c.default_sr

        self.dorks = bool( dorks_re.match(self.url) )
        Wrapped.__init__(self, link)
        if link is None:
            self.add_props(c.user, [self])
    
    @classmethod
    def add_props(cls, user, wrapped):
        # unlike most wrappers we can guarantee that there is a link
        # that this wrapper is wrapping.
        nonempty = [w for w in wrapped if hasattr(w, "_fullname")]
        Link.add_props(user, nonempty)
        for w in wrapped:
            w.score_fmt = Score.points
            if not hasattr(w, '_fullname'):
                w._fullname = None
                w.tblink = add_sr("/s/"+quote(w.url))
                submit_url_options = dict(url  = _force_unicode(w.url),
                                          then = 'tb')
                if w.title:
                    submit_url_options['title'] = _force_unicode(w.title)
                w.submit_url = add_sr('/submit' +
                                         query_string(submit_url_options))
            else:
                w.tblink = add_sr("/tb/"+w._id36)
                w.upstyle = "mod" if w.likes else ""
                w.downstyle = "mod" if w.likes is False else ""
            if not c.user_is_loggedin:
                w.loginurl = add_sr("/login?dest="+quote(w.tblink))
        # run to set scores with current score format (for example)
        Printable.add_props(user, nonempty)



class NewLink(Templated):
    """Render the link submission form"""
    def __init__(self, captcha = None, url = '', title= '', subreddits = (),
                 then = 'comments'):
        tabs = (('link', ('link-desc', 'url-field')),
                ('text', ('text-desc', 'text-field')))
        all_fields = set(chain(*(parts for (tab, parts) in tabs)))
        buttons = []
        self.default_tabs = tabs[0][1]
        self.default_tab = tabs[0][0]
        for tab_name, parts in tabs:
            to_show = ','.join('#' + p for p in parts)
            to_hide = ','.join('#' + p for p in all_fields if p not in parts)
            onclick = "return select_form_tab(this, '%s', '%s');"
            onclick = onclick % (to_show, to_hide)
            if tab_name == self.default_tab:
                self.default_show = to_show
                self.default_hide = to_hide

            buttons.append(JsButton(tab_name, onclick=onclick, css_class=tab_name))

        self.formtabs_menu = JsNavMenu(buttons, type = 'formtab')
        self.default_tabs = tabs[0][1]

        self.sr_searches = simplejson.dumps(popular_searches())

        if isinstance(c.site, FakeSubreddit):
            self.default_sr = subreddits[0] if subreddits else g.default_sr
        else:
            self.default_sr = c.site.name

        Templated.__init__(self, captcha = captcha, url = url,
                         title = title, subreddits = subreddits,
                         then = then)

class ShareLink(CachedTemplate):
    def __init__(self, link_name = "", emails = None):
        self.captcha = c.user.needs_captcha()
        self.email = getattr(c.user, 'email', "")
        self.username = c.user.name
        Templated.__init__(self, link_name = link_name,
                           emails = c.user.recent_share_emails())

        

class Share(Templated):
    pass

class Mail_Opt(Templated):
    pass

class OptOut(Templated):
    pass

class OptIn(Templated):
    pass


class ButtonEmbed(CachedTemplate):
    """Generates the JS wrapper around the buttons for embedding."""
    def __init__(self, button = None, width = 100,
                 height=100, referer = "", url = "", **kw):
        arg = "cnameframe=1&" if c.cname else ""
        sr = c.site.name if not isinstance(c.site, FakeSubreddit) else ""
        if sr:
            arg += "sr=%s&" % sr
        Templated.__init__(self, button = button,
                           width = width, height = height,
                           referer=referer, url = url,
                           domain = get_domain(subreddit = False),
                           arg = arg,
                           **kw)

class Button(Wrapped):
    cachable = True
    extension_handling = False
    def __init__(self, link, **kw):
        Wrapped.__init__(self, link, **kw)
        if link is None:
            self.title = ""
            self.add_props(c.user, [self])


    @classmethod
    def add_props(cls, user, wrapped):
        # unlike most wrappers we can guarantee that there is a link
        # that this wrapper is wrapping.
        Link.add_props(user, [w for w in wrapped if hasattr(w, "_fullname")])
        for w in wrapped:
            # caching: store the user name since each button has a modhash
            w.user_name = c.user.name if c.user_is_loggedin else ""
            if not hasattr(w, '_fullname'):
                w._fullname = None

    def render(self, *a, **kw):
        res = Wrapped.render(self, *a, **kw)
        return responsive(res, True)

class ButtonLite(Button):
    def render(self, *a, **kw):
        return Wrapped.render(self, *a, **kw)

class ButtonNoBody(Button):
    """A button page that just returns the raw button for direct embeding"""
    pass

class ButtonDemoPanel(Templated):
    """The page for showing the different styles of embedable voting buttons"""
    pass

class UpgradeButtons(Templated):
    """The page for showing the different styles of embedable voting buttons"""
    pass


class SelfServeBlurb(Templated):
    pass

class FeedbackBlurb(Templated):
    pass

class Feedback(Templated):
    """The feedback and ad inquery form(s)"""
    def __init__(self, title, action):
        email = name = ''
        if c.user_is_loggedin:
            email = getattr(c.user, "email", "")
            name = c.user.name

        captcha = None
        if not c.user_is_loggedin or c.user.needs_captcha():
            captcha = Captcha()

        Templated.__init__(self,
                         captcha = captcha,
                         title = title,
                         action = action,
                         email = email,
                         name = name)


class WidgetDemoPanel(Templated):
    """Demo page for the .embed widget."""
    pass

class Bookmarklets(Templated):
    """The bookmarklets page."""
    def __init__(self, buttons=None):
        if buttons is None:
            buttons = ["submit", "serendipity!"]
            # only include the toolbar link if we're not on an
            # unathorised cname. See toolbar.py:GET_s for discussion
            if not (c.cname and c.site.domain not in g.authorized_cnames):
                buttons.insert(0, "reddit toolbar")
        Templated.__init__(self, buttons = buttons)


class Translator_Message(Templated):
    def __init__(self, locale, user):
        from r2.lib.translation import Translator
        self.user = user
        self.locale = locale
        self.lang_name = Translator.get_name(self.locale)
        self.en_name = Translator.get_en_name(self.locale)
        Templated.__init__(self)

class AdminTranslations(Templated):
    """The translator control interface, used for determining which
    user is allowed to edit which translation file and for providing a
    summary of what translation files are done and/or in use."""
    def __init__(self):
        from r2.lib.translation import list_translations
        Templated.__init__(self)
        self.translations = list_translations()

class UserAwards(Templated):
    """For drawing the regular-user awards page."""
    def __init__(self):
        from r2.models import Award, Trophy
        Templated.__init__(self)

        self.regular_winners = []
        self.manuals = []
        self.invisibles = []

        for award in Award._all_awards():
            if award.awardtype == 'regular':
                trophies = Trophy.by_award(award)
                # Don't show awards that nobody's ever won
                # (e.g., "9-Year Club")
                if trophies:
                    winner = trophies[0]._thing1.name
                    self.regular_winners.append( (award, winner, trophies[0]) )
            elif award.awardtype == 'manual':
                self.manuals.append(award)
            elif award.awardtype == 'invisible':
                self.invisibles.append(award)
            else:
                raise NotImplementedError

class AdminErrorLog(Templated):
    """The admin page for viewing the error log"""
    def __init__(self):
        hcb = g.hardcache.backend

        date_groupings = {}
        hexkeys_seen = {}

        idses = hcb.ids_by_category("error")
        errors = g.hardcache.get_multi(prefix="error-", keys=idses)

        for ids in idses:
            date, hexkey = ids.split("-")

            hexkeys_seen[hexkey] = True

            d = errors.get(ids, None)

            if d is None:
                log_text("error=None", "Why is error-%s None?" % ids,
                         "warning")
                continue

            tpl = (len(d['occurrences']), hexkey, d)
            date_groupings.setdefault(date, []).append(tpl)

        self.nicknames = {}
        self.statuses = {}

        nicks = g.hardcache.get_multi(prefix="error_nickname-",
                                      keys=hexkeys_seen.keys())
        stati = g.hardcache.get_multi(prefix="error_status-",
                                      keys=hexkeys_seen.keys())

        for hexkey in hexkeys_seen.keys():
            self.nicknames[hexkey] = nicks.get(hexkey, "???")
            self.statuses[hexkey] = stati.get(hexkey, "normal")

        idses = hcb.ids_by_category("logtext")
        texts = g.hardcache.get_multi(prefix="logtext-", keys=idses)

        for ids in idses:
            date, level, classification = ids.split("-", 2)
            textoccs = []
            dicts = texts.get(ids, None)
            if dicts is None:
                log_text("logtext=None", "Why is logtext-%s None?" % ids,
                         "warning")
                continue
            for d in dicts:
                textoccs.append( (d['text'], d['occ'] ) )

            sort_order = {
                'error': -1,
                'warning': -2,
                'info': -3,
                'debug': -4,
                }[level]

            tpl = (sort_order, level, classification, textoccs)
            date_groupings.setdefault(date, []).append(tpl)

        self.date_summaries = []

        for date in sorted(date_groupings.keys(), reverse=True):
            groupings = sorted(date_groupings[date], reverse=True)
            self.date_summaries.append( (date, groupings) )

        Templated.__init__(self)

class AdminAds(Templated):
    """The admin page for editing ads"""
    def __init__(self):
        from r2.models import Ad
        Templated.__init__(self)
        self.ads = Ad._all_ads()

class AdminAdAssign(Templated):
    """The interface for assigning an ad to a community"""
    def __init__(self, ad):
        self.weight = 100
        Templated.__init__(self, ad = ad)

class AdminAdSRs(Templated):
    """View the communities an ad is running on"""
    def __init__(self, ad):
        self.adsrs = AdSR.by_ad(ad)

        # Create a dictionary of
        #       SR => total weight of all its ads
        # for all SRs that this ad is running on
        self.sr_totals = {}
        for adsr in self.adsrs:
            sr = adsr._thing2

            if sr.name not in self.sr_totals:
                # We haven't added up this SR yet.
                self.sr_totals[sr.name] = 0
                # Get all its ads and total them up.
                sr_adsrs = AdSR.by_sr_merged(sr)
                for adsr2 in sr_adsrs:
                    self.sr_totals[sr.name] += adsr2.weight

        Templated.__init__(self, ad = ad)

class AdminAwards(Templated):
    """The admin page for editing awards"""
    def __init__(self):
        from r2.models import Award
        Templated.__init__(self)
        self.awards = Award._all_awards()

class AdminAwardGive(Templated):
    """The interface for giving an award"""
    def __init__(self, award):
        now = datetime.datetime.now(g.display_tz)
        if award.awardtype == 'regular':
            self.description = "??? -- " + now.strftime("%Y-%m-%d")
        else:
            self.description = ""
        self.url = ""

        Templated.__init__(self, award = award)

class AdminAwardWinners(Templated):
    """The list of winners of an award"""
    def __init__(self, award):
        trophies = Trophy.by_award(award)
        Templated.__init__(self, award = award, trophies = trophies)

class AdminUsage(Templated):
    """The admin page for viewing usage stats"""
    def __init__(self):
        hcb = g.hardcache.backend

        self.actions = {}
        triples = set() # sorting key
        daily_stats = {}

        idses = hcb.ids_by_category("profile_count", limit=10000)
        counts   = g.hardcache.get_multi(prefix="profile_count-", keys=idses)
        elapseds = g.hardcache.get_multi(prefix="profile_elapsed-", keys=idses)

        # The next three code paragraphs are for the case where we're
        # rendering the current period and trying to decide what load class
        # to use. For example, if today's number of hits equals yesterday's,
        # and we're 23:59 into the day, that's totally normal. But if we're
        # only 12 hours into the day, that's twice what we'd expect. So
        # we're going to scale the current period by the percent of the way
        # into the period that we are.
        #
        # If we're less than 5% of the way into the period, we skip this
        # step. This both avoids Div0 errors and keeps us from extrapolating
        # ridiculously from a tiny sample size.

        now = c.start_time.astimezone(g.display_tz)
        t_midnight = trunc_time(now, hours=24, mins=60)
        t_hour = trunc_time(now, mins=60)
        t_5min = trunc_time(now, mins=5)

        offset_day  = (now - t_midnight).seconds / 86400.0
        offset_hour = (now - t_hour).seconds     / 3600.0
        offset_5min = (now - t_5min).seconds     / 300.0

        this_day  = t_midnight.strftime("%Y/%m/%d_xx:xx")
        this_hour =     t_hour.strftime("%Y/%m/%d_%H:xx")
        this_5min =     t_5min.strftime("%Y/%m/%d_%H:%M")

        for ids in idses:
            time, action = ids.split("-")

            # coltype strings are carefully chosen to sort alphabetically
            # in the order that they do

            if time.endswith("xx:xx"):
                coltype = 'Day'
                factor = 1.0
                label = time[5:10] # MM/DD
                if time == this_day and offset_day > 0.05:
                    factor /= offset_day
            elif time.endswith(":xx"):
                coltype = 'Hour'
                factor = 24.0
                label = time[11:] # HH:xx
                if time == this_hour and offset_hour > 0.05:
                    factor /= offset_hour
            else:
                coltype = 'five-min'
                factor = 288.0 # number of five-minute periods in a day
                label = time[11:] # HH:MM
                if time == this_5min and offset_5min > 0.05:
                    factor /= offset_5min

            count = counts.get(ids, None)
            if count is None or count == 0:
                log_text("usage count=None", "For %r, it's %r" % (ids, count), "error")
                continue

            # Elapsed in hardcache is in hundredths of a second.
            # Multiply it by 100 so from this point forward, we're
            # dealing with seconds -- as floats with two decimal
            # places of precision. Similarly, round the average
            # to two decimal places.
            elapsed = elapseds.get(ids, 0) / 100.0
            average = int(100.0 * elapsed / count) / 100.0

            # Again, the "triple" tuples are a sorting key for the columns
            triples.add( (coltype, time, label) )

            if coltype == 'Day':
                daily_stats.setdefault(action, []).append(
                    (count, elapsed, average)
                    )

            self.actions.setdefault(action, {})
            self.actions[action][label] = dict(count=count, elapsed=elapsed,
                                               average=average,
                                               factor=factor,
                                               classes = {})

        # Figure out what a typical day looks like. For each action,
        # look at the daily stats and record the median.
        for action in daily_stats.keys():
            if len(daily_stats[action]) < 2:
                # This is a new action. No point in guessing what normal
                # load for it looks like.
                continue
            med = {}
            med["count"]   = median([ x[0] for x in daily_stats[action] ])
            med["elapsed"] = median([ x[1] for x in daily_stats[action] ])
            med["average"] = median([ x[2] for x in daily_stats[action] ])

            # For the purposes of load classes, round the baseline count up
            # to 5000 times per day, the elapsed to 30 minutes per day, and
            # the average to 0.10 seconds per request. This not only avoids
            # division-by-zero problems but also means that if something
            # went from taking 0.01 seconds per day to 0.08 seconds per day,
            # we're not going to consider it an emergency.
            med["count"]   = max(5000,   med["count"])
            med["elapsed"] = max(1800.0, med["elapsed"])
            med["average"] = max(0.10,  med["average"])

#            print "Median count for %s is %r" % (action, med["count"])

            for d in self.actions[action].values():
                ice_cold = False
                for category in ("elapsed", "count", "average"):
                    if category == "average":
                        scaled = d[category]
                    else:
                        scaled = d[category] * d["factor"]

                    if category == "elapsed" and scaled < 5 * 60:
                        # If we're spending less than five mins a day
                        # on this operation, consider it ice cold regardless
                        # of how much of an outlier it is
                        ice_cold = True

                    if ice_cold:
                        d["classes"][category] = "load0"
                        continue

                    if med[category] <= 0:
                        d["classes"][category] = "load9"
                        continue

                    ratio = scaled / med[category]
                    if ratio > 5.0:
                        d["classes"][category] = "load9"
                    elif ratio > 3.0:
                        d["classes"][category] = "load8"
                    elif ratio > 2.0:
                        d["classes"][category] = "load7"
                    elif ratio > 1.5:
                        d["classes"][category] = "load6"
                    elif ratio > 1.1:
                        d["classes"][category] = "load5"
                    elif ratio > 0.9:
                        d["classes"][category] = "load4"
                    elif ratio > 0.75:
                        d["classes"][category] = "load3"
                    elif ratio > 0.5:
                        d["classes"][category] = "load2"
                    elif ratio > 0.10:
                        d["classes"][category] = "load1"
                    else:
                        d["classes"][category] = "load0"

        # Build a list called labels that gives the template a sorting
        # order for the columns.
        self.labels = []
        # Keep track of how many times we've seen a granularity (i.e., coltype)
        # so we can hide any that come after the third
        coltype_counts = {}
        # sort actions by whatever will end up as the first column
        action_sorting_column = None
        for coltype, time, label in sorted(triples, reverse=True):
            if action_sorting_column is None:
                action_sorting_column = label
            coltype_counts.setdefault(coltype, 0)
            coltype_counts[coltype] += 1
            self.labels.append( (label, coltype_counts[coltype] > 3) )

        self.action_order = sorted(self.actions.keys(), reverse=True,
                key = lambda x:
                      self.actions[x].get(action_sorting_column, {"elapsed":0})["elapsed"])

        Templated.__init__(self)

class Ads(Templated):
    pass

class Admin_Rightbox(Templated):
    pass

class Embed(Templated):
    """wrapper for embedding /help into reddit as if it were not on a separate wiki."""
    def __init__(self,content = ''):
        Templated.__init__(self, content = content)


class Page_down(Templated):
    def __init__(self, **kw):
        message = kw.get('message', _("This feature is currently unavailable. Sorry"))
        Templated.__init__(self, message = message)

class WrappedUser(CachedTemplate):
    def __init__(self, user, attribs = [], context_thing = None, gray = False):
        attribs.sort()
        author_cls = 'author'

        if gray:
            author_cls += ' gray'
        for tup in attribs:
            author_cls += " " + tup[2]

        target = None
        ip_span = None
        context_deleted = None
        if context_thing:
            target = getattr(context_thing, 'target', None)
            ip_span = getattr(context_thing, 'ip_span', None)
            context_deleted = context_thing.deleted

        karma = ''
        if c.user_is_admin:
            karma = ' (%d)' % user.link_karma

        CachedTemplate.__init__(self,
                                name = user.name,
                                author_cls = author_cls,
                                attribs = attribs,
                                context_thing = context_thing,
                                karma = karma,
                                ip_span = ip_span,
                                context_deleted = context_deleted,
                                fullname = user._fullname,
                                user_deleted = user._deleted)

# Classes for dealing with friend/moderator/contributor/banned lists


class UserTableItem(Templated):
    """A single row in a UserList of type 'type' and of name
    'container_name' for a given user.  The provided list of 'cells'
    will determine what order the different columns are rendered in.
    Currently, this list can consist of 'user', 'sendmessage' and
    'remove'."""
    def __init__(self, user, type, cellnames, container_name, editable,
                 remove_action):
        self.user, self.type, self.cells = user, type, cellnames
        self.container_name = container_name
        self.editable       = editable
        self.remove_action  = remove_action
        Templated.__init__(self)

    def __repr__(self):
        return '<UserTableItem "%s">' % self.user.name

class UserList(Templated):
    """base class for generating a list of users"""    
    form_title     = ''
    table_title    = ''
    type           = ''
    container_name = ''
    cells          = ('user', 'sendmessage', 'remove')
    _class         = ""
    destination    = "friend"
    remove_action  = "unfriend"
    editable_fn    = None

    def __init__(self, editable = True):
        self.editable = editable
        Templated.__init__(self)

    def user_row(self, user):
        """Convenience method for constructing a UserTableItem
        instance of the user with type, container_name, etc. of this
        UserList instance"""
        editable = self.editable

        if self.editable_fn and not self.editable_fn(user):
            editable = False

        return UserTableItem(user, self.type, self.cells, self.container_name,
                             editable, self.remove_action)

    @property
    def users(self, site = None):
        """Generates a UserTableItem wrapped list of the Account
        objects which should be present in this UserList."""
        uids = self.user_ids()
        if uids:
            users = Account._byID(uids, True, return_dict = False) 
            return [self.user_row(u) for u in users]
        else:
            return []

    def user_ids(self):
        """virtual method for fetching the list of ids of the Accounts
        to be listing in this UserList instance"""
        raise NotImplementedError

    @property
    def container_name(self):
        return c.site._fullname

class FriendList(UserList):
    """Friend list on /pref/friends"""
    type = 'friend'

    @property
    def form_title(self):
        return _('add a friend')

    @property
    def table_title(self):
        return _('your friends')

    def user_ids(self):
        return c.user.friends

    @property
    def container_name(self):
        return c.user._fullname

class ContributorList(UserList):
    """Contributor list on a restricted/private reddit."""
    type = 'contributor'

    @property
    def form_title(self):
        return _("add approved submitter")

    @property
    def table_title(self):
        return _("approved submitters for %(reddit)s") % dict(reddit = c.site.name)

    def user_ids(self):
        return c.site.contributors

class ModList(UserList):
    """Moderator list for a reddit."""
    type = 'moderator'

    @property
    def form_title(self):
        return _('add moderator')

    @property
    def table_title(self):
        return _("moderators to %(reddit)s") % dict(reddit = c.site.name)

    def editable_fn(self, user):
        if not c.user_is_loggedin:
            return False
        elif c.user_is_admin:
            return True
        else:
            return c.site.can_demod(c.user, user)

    def user_ids(self):
        return c.site.moderators

class BannedList(UserList):
    """List of users banned from a given reddit"""
    type = 'banned'

    @property
    def form_title(self):
        return _('ban users')

    @property
    def table_title(self):
        return  _('banned users')

    def user_ids(self):
        return c.site.banned

class TrafficViewerList(UserList):
    """Traffic share list on /traffic/*"""
    destination = "traffic_viewer"
    remove_action = "rm_traffic_viewer"
    type = 'traffic'

    def __init__(self, link, editable = True):
        self.link = link
        UserList.__init__(self, editable = editable)

    @property
    def form_title(self):
        return _('share traffic')

    @property
    def table_title(self):
        return _('current viewers')

    def user_ids(self):
        return promote.traffic_viewers(self.link)

    @property
    def container_name(self):
        return self.link._fullname



class DetailsPage(LinkInfoPage):
    extension_handling= False

    def content(self):
        # TODO: a better way?
        from admin_pages import Details
        return self.content_stack((self.link_listing, Details(link = self.link)))

class Cnameframe(Templated):
    """The frame page."""
    def __init__(self, original_path, subreddit, sub_domain):
        Templated.__init__(self, original_path=original_path)
        if sub_domain and subreddit and original_path:
            self.title = "%s - %s" % (subreddit.title, sub_domain)
            u = UrlParser(subreddit.path + original_path)
            u.hostname = get_domain(cname = False, subreddit = False)
            u.update_query(**request.get.copy())
            u.put_in_frame()
            self.frame_target = u.unparse()
        else:
            self.title = ""
            self.frame_target = None

class FrameBuster(Templated):
    pass

class SelfServiceOatmeal(Templated):
    pass

class PromotePage(Reddit):
    create_reddit_box  = False
    submit_box         = False
    extension_handling = False
    searchbox          = False

    def __init__(self, title, nav_menus = None, *a, **kw):
        buttons = [NamedButton('new_promo')]
        if c.user_is_sponsor:
            buttons.append(NamedButton('roadblock'))
            buttons.append(NamedButton('current_promos', dest = ''))
        else:
            buttons.append(NamedButton('my_current_promos', dest = ''))

        buttons += [NamedButton('future_promos'),
                    NamedButton('unpaid_promos'),
                    NamedButton('rejected_promos'),
                    NamedButton('pending_promos'),
                    NamedButton('live_promos'),
                    NamedButton('graph')]

        menu  = NavMenu(buttons, base_path = '/promoted',
                        type='flatlist')

        if nav_menus:
            nav_menus.insert(0, menu)
        else:
            nav_menus = [menu]

        kw['show_sidebar'] = False
        Reddit.__init__(self, title, nav_menus = nav_menus, *a, **kw)

class PromoteLinkForm(Templated):
    def __init__(self, sr = None, link = None, listing = '',
                 timedeltatext = '', *a, **kw):
        bids = []
        if c.user_is_sponsor and link:
            self.author = Account._byID(link.author_id)
            try:
                bids = bidding.Bid.lookup(thing_id = link._id)
                bids.sort(key = lambda x: x.date, reverse = True)
            except NotFound:
                pass

        # reference "now" to what we use for promtions
        now = promote.promo_datetime_now()

        # min date is the day before the first possible start date.
        mindate = (make_offset_date(now, g.min_promote_future,
                                    business_days = True) -
                   datetime.timedelta(1))

        startdate = mindate + datetime.timedelta(1)
        enddate   = startdate + datetime.timedelta(3)

        self.startdate = startdate.strftime("%m/%d/%Y")
        self.enddate   = enddate  .strftime("%m/%d/%Y")

        self.mindate   = mindate  .strftime("%m/%d/%Y")

        self.link = None
        if link:
            self.sr_searches = simplejson.dumps(popular_searches())
            self.subreddits = (Subreddit.submit_sr_names(c.user) or
                               Subreddit.submit_sr_names(None))
            self.default_sr = self.subreddits[0] if self.subreddits \
                              else g.default_sr
            # have the promo code wrap the campaigns for rendering
            self.link = promote.editable_add_props(link)

        if not c.user_is_sponsor:
            self.now = promote.promo_datetime_now().date()
            start_date = promote.promo_datetime_now(offset = -14).date()
            end_date = promote.promo_datetime_now(offset = 14).date()
            self.promo_traffic = dict(load_traffic('day', 'promos'))
            self.market, self.promo_counter = \
                Promote_Graph.get_market(None, start_date, end_date)

        Templated.__init__(self, sr = sr, 
                           datefmt = datefmt,
                           timedeltatext = timedeltatext,
                           listing = listing, bids = bids, 
                           *a, **kw)

class PromoteLinkFormOld(PromoteLinkForm):
    def __init__(self, **kw):
        PromoteLinkForm.__init__(self, **kw)
        self.bid = g.min_promote_bid
        campaign = {}
        if self.link:
            campaign = self.link.campaigns[0]
            self.startdate = campaign.start_date
            self.enddate = campaign.end_date

        self.bid = campaign.get("bid", g.min_promote_bid)
        self.freebie = campaign.get("status",{}).get("free", False)
        self.complete = campaign.get("status",{}).get("complete", False)
        self.paid = campaign.get("status",{}).get("paid", False)

class Roadblocks(Templated):
    def __init__(self):
        self.roadblocks = promote.get_roadblocks()
        Templated.__init__(self)
        # reference "now" to what we use for promtions
        now = promote.promo_datetime_now()

        startdate = now + datetime.timedelta(1)
        enddate   = startdate + datetime.timedelta(1)

        self.startdate = startdate.strftime("%m/%d/%Y")
        self.enddate   = enddate  .strftime("%m/%d/%Y")
        self.sr_searches = simplejson.dumps(popular_searches())
        self.subreddits = (Subreddit.submit_sr_names(c.user) or
                           Subreddit.submit_sr_names(None))
        self.default_sr = self.subreddits[0] if self.subreddits \
                          else g.default_sr

class TabbedPane(Templated):
    def __init__(self, tabs):
        """Renders as tabbed area where you can choose which tab to
        render. Tabs is a list of tuples (tab_name, tab_pane)."""
        buttons = []
        for tab_name, title, pane in tabs:
            buttons.append(JsButton(title, onclick="return select_tab_menu(this, '%s');" % tab_name))

        self.tabmenu = JsNavMenu(buttons, type = 'tabpane')
        self.tabs = tabs

        Templated.__init__(self)

class LinkChild(object):
    def __init__(self, link, load = False, expand = False, nofollow = False):
        self.link = link
        self.expand = expand
        self.load = load or expand
        self.nofollow = nofollow

    def content(self):
        return ''

def make_link_child(item):
    link_child = None
    editable = False

    # if the item has a media_object, try to make a MediaEmbed for rendering
    if item.media_object:
        media_embed = None
        if isinstance(item.media_object, basestring):
            media_embed = item.media_object
        else:
            media_embed = get_media_embed(item.media_object)
            if media_embed:
                media_embed =  MediaEmbed(media_domain = g.media_domain,
                                          height = media_embed.height + 10,
                                          width = media_embed.width + 10,
                                          scrolling = media_embed.scrolling,
                                          id36 = item._id36)
            else:
                g.log.debug("media_object without media_embed %s" % item)

        if media_embed:
            link_child = MediaChild(item, media_embed, load = True)

    # if the item has selftext, add a selftext child
    elif item.selftext:
        expand = getattr(item, 'expand_children', False)
        link_child = SelfTextChild(item, expand = expand,
                                   nofollow = item.nofollow)
        #draw the edit button if the contents are pre-expanded
        editable = (expand and
                    item.author == c.user and
                    not item._deleted)

    return link_child, editable


class MediaChild(LinkChild):
    """renders when the user hits the expando button to expand media
       objects, like embedded videos"""
    css_style = "video"
    def __init__(self, link, content, **kw):
        self._content = content
        LinkChild.__init__(self, link, **kw)

    def content(self):
        if isinstance(self._content, basestring):
            return self._content
        return self._content.render()

class MediaEmbed(Templated):
    """The actual rendered iframe for a media child"""
    pass

class SelfTextChild(LinkChild):
    css_style = "selftext"
    def content(self):
        u = UserText(self.link, self.link.selftext,
                     editable = c.user == self.link.author,
                     nofollow = self.nofollow)
        return u.render()

class UserText(CachedTemplate):
    def __init__(self,
                 item,
                 text = '',
                 have_form = True,
                 editable = False,
                 creating = False,
                 nofollow = False,
                 target = None,
                 display = True,
                 post_form = 'editusertext',
                 cloneable = False,
                 extra_css = '',
                 name = "text"):

        css_class = "usertext"
        if cloneable:
            css_class += " cloneable"
        if extra_css:
            css_class += " " + extra_css

        if text is None:
            text = ''

        CachedTemplate.__init__(self,
                                fullname = item._fullname if item else "", 
                                text = text,
                                have_form = have_form,
                                editable = editable,
                                creating = creating,
                                nofollow = nofollow,
                                target = target,
                                display = display,
                                post_form = post_form,
                                cloneable = cloneable,
                                css_class = css_class,
                                name = name)

class MediaEmbedBody(CachedTemplate):
    """What's rendered inside the iframe that contains media objects"""
    def render(self, *a, **kw):
        res = CachedTemplate.render(self, *a, **kw)
        return responsive(res, True)

class Traffic(Templated):
    @staticmethod
    def slice_traffic(traffic, *indices):
        return [[a] + [b[i] for i in indices] for a, b in traffic]


class PromotedTraffic(Traffic):
    """
    Traffic page for a promoted link, including 2 graphs (one for
    impressions and one for clicks with uniques on each plotted in
    multiy format) and a table of the data.
    """
    def __init__(self, thing):
        # TODO: needs a fix for multiple campaigns
        self.thing = thing
        d = until = None
        self.traffic = []
        if thing.campaigns:
            d = min(sd.date() if isinstance(sd, datetime.datetime) else sd
                     for sd, ed, bid, sr, trans_id in thing.campaigns.values()
                     if trans_id)
            until = max(ed.date() if isinstance(ed, datetime.datetime) else ed
                     for sd, ed, bid, sr, trans_id in thing.campaigns.values()
                     if trans_id)
            now = datetime.datetime.now(g.tz).date()

            # the results are preliminary until 1 day after the promotion ends
            self.preliminary = (until + datetime.timedelta(1) > now)
            self.traffic = load_traffic('hour', "thing", thing._fullname,
                                        start_time = d, stop_time = until)

            # load monthly totals if we have them, otherwise use the daily totals
            self.totals =  load_traffic('month', "thing", thing._fullname)
            if not self.totals:
                self.totals = load_traffic('day', "thing", thing._fullname)
            # generate a list of
            # (uniq impressions, # impressions, uniq clicks, # clicks)
            if self.totals:
                self.totals = map(sum, zip(*zip(*self.totals)[1]))

        imp = self.slice_traffic(self.traffic, 0, 1)

        if len(imp) > 2:
            imp_total = sum(x[2] for x in imp)
            # ensure total consistency:
            if self.totals:
                self.totals[1] = imp_total

            imp_total = locale.format('%d', imp_total, True)
            
            self.imp_graph = TrafficGraph(imp[-72:], ylabels = ['uniques', 'total'],
                                          title = ("recent impressions (%s total)" %
                                                   imp_total))
            cli = self.slice_traffic(self.traffic, 2, 3)
            cli_total = sum(x[2] for x in cli)
            # ensure total consistency
            if self.totals:
                self.totals[3] = cli_total
            cli_total = locale.format('%d', cli_total, True)
            self.cli_graph = TrafficGraph(cli[-72:], ylabels = ['uniques', 'total'],
                                          title = ("recent clicks (%s total)" %
                                                   cli_total))

        else:
            self.imp_graph = self.cli_graph = None

        editable = c.user_is_sponsor or c.user._id == thing.author_id
        self.viewers = TrafficViewerList(thing, editable = editable)
        Templated.__init__(self)

    def to_iter(self, localize = True, total = False):
        def num(x):
            if localize:
                return locale.format('%d', x, True)
            return str(x)
        def row(label, data):
            uimp, nimp, ucli, ncli = data
            return (label,
                   num(uimp), num(nimp), num(ucli), num(ncli),
                   ("%.2f%%" % (float(100*ucli) / uimp)) if nimp else "--.--%", 
                   ("%.2f%%" % (float(100*ncli) / nimp)) if nimp else "--.--%")

        for date, data in self.traffic:
            yield row(date.strftime("%Y-%m-%d %H:%M"), data)
        if total:
            yield row("total", self.totals)


    def as_csv(self):
        return to_csv(self.to_iter(localize = False, total = True))

class RedditTraffic(Traffic):
    """
    fetches hourly and daily traffic for the current reddit.  If the
    current reddit is a default subreddit, fetches the site-wide
    uniques and includes monthly totals.  In this latter case, getter
    methods are available for computing breakdown of site trafffic by
    reddit.
    """
    def __init__(self):
        self.has_data = False
        ivals = ["hour", "day"]
        if c.default_sr:
            ivals.append("month")

        for ival in ivals:
            if c.default_sr:
                data = load_traffic(ival, "total", "")
            else:
                data = load_traffic(ival, "reddit", c.site.name)
            if not data:
                break
            slices = [("uniques",     (0, 2) if c.site.domain else (0,),
                       "FF4500"),
                      ("impressions", (1, 3) if c.site.domain else (1,),
                       "336699")]
            if not c.default_sr and ival == 'day':
                slices.append(("subscriptions", (4,), "00FF00"))
            setattr(self, ival + "_data", data)
            for name, indx, color in slices:
                data2 = self.slice_traffic(data, *indx)
                setattr(self, name + "_" + ival + "_chart", data2)
                title = "%s by %s" % (name, ival)
                res = TrafficGraph(data2, colors = [color], title = title)
                setattr(self, name + "_" + ival, res)
        else:
            self.has_data = True
        if self.has_data:
            imp_by_day = [[] for i in range(7)]
            uni_by_day = [[] for i in range(7)]
            if c.site.domain:
                dates, imps, foo    = zip(*self.impressions_day_chart)
                dates, uniques, foo = zip(*self.uniques_day_chart)
            else:
                dates, imps    = zip(*self.impressions_day_chart)
                dates, uniques = zip(*self.uniques_day_chart)
            self.uniques_mean     = sum(map(float, uniques))/len(uniques)
            self.impressions_mean = sum(map(float, imps))/len(imps)
            for i, d in enumerate(dates):
                imp_by_day[d.weekday()].append(float(imps[i]))
                uni_by_day[d.weekday()].append(float(uniques[i]))
            self.uniques_by_dow     = [sum(x)/max(len(x),1)
                                       for x in uni_by_day]
            self.impressions_by_dow = [sum(x)/max(len(x),1)
                                       for x in imp_by_day]
        Templated.__init__(self)

    def reddits_summary(self):
        if c.default_sr:
            data = map(list, load_summary("reddit"))
            data.sort(key = lambda x: x[1][1], reverse = True)
            for d in data:
                name = d[0]
                for sr in (Default, Friends, All, Sub):
                    if name == sr.name:
                        name = sr
                        break
                else:
                    try:
                        name = Subreddit._by_name(name)
                    except NotFound:
                        name = DomainSR(name)
                d[0] = name
            return data
        return res

    def monthly_summary(self):
        """
        Convenience method b/c it is bad form to do this much math
        inside of a template.b
        """
        res = []
        if c.default_sr:
            data = self.month_data

            # figure out the mean number of users last month, 
            # unless today is the first and there is no data
            days = self.day_data
            now = datetime.datetime.utcnow()
            if now.day != 1:
                # project based on traffic so far
                # totals are going to be up to yesterday
                month_len = calendar.monthrange(now.year, now.month)[1]

                lastmonth = datetime.datetime.utcnow().month
                lastmonthyear = datetime.datetime.utcnow().year
                if lastmonth == 1:
                    lastmonthyear -= 1
                    lastmonth = 1
                else:
                    lastmonth = (lastmonth - 1) if lastmonth != 1 else 12
                # length of last month
                lastmonthlen = calendar.monthrange(lastmonthyear, lastmonth)[1]
    
                lastdays = filter(lambda x: x[0].month == lastmonth, days) 
                thisdays = filter(lambda x: x[0].month == now.month, days) 
                user_scale = 0
                if lastdays:
                    last_mean = (sum(u for (d, (u, v)) in lastdays) / 
                                 float(len(lastdays)))
                    day_mean = (sum(u for (d, (u, v)) in thisdays) / 
                                float(len(thisdays)))
                    if last_mean and day_mean:
                        user_scale = ( (day_mean * month_len) /
                                       (last_mean * lastmonthlen) )
            last_month_users = 0
            for x, (date, d) in enumerate(data):
                res.append([("date", date.strftime("%Y-%m")),
                            ("", locale.format("%d", d[0], True)),
                            ("", locale.format("%d", d[1], True))])
                last_d = data[x-1][1] if x else None
                for i in range(2):
                    # store last month's users for this month's projection
                    if x == len(data) - 2 and i == 0:
                        last_month_users = d[i]
                    if x == 0:
                        res[-1].append(("",""))
                    # project, unless today is the first of the month
                    elif x == len(data) - 1 and now.day != 1:
                        # yesterday
                        yday = (datetime.datetime.utcnow()
                                -datetime.timedelta(1)).day
                        if i == 0:
                            scaled = int(last_month_users * user_scale)
                        else:
                            scaled = float(d[i] * month_len) / yday
                        res[-1].append(("gray",
                                        locale.format("%d", scaled, True)))
                    elif last_d and d[i] and last_d[i]:
                        f = 100 * (float(d[i])/last_d[i] - 1)

                        res[-1].append(("up" if f > 0 else "down", 
                                        "%5.2f%%" % f))
        return res

class TrafficGraph(Templated):
    def __init__(self, data, width = 300, height = 175,
                 bar_fmt = True, colors = ("FF4500", "336699"), title = '',
                 ylabels = [], multiy = True):
        # fallback on google charts
        chart = graph.LineGraph(data[:72], colors = colors)
        self.gc = chart.google_chart(ylabels = ylabels, multiy = multiy, title = title)

        xdata = []
        ydata = []
        for d in data:
            xdata.append(time.mktime(d[0].timetuple())*1000)
            ydata.append(d[1:])
        ydata = zip(*ydata)
        self.colors = colors
        self.title = title

        if bar_fmt:
            xdata = graph.DataSeries(xdata).toBarX()

        if ydata and not isinstance(ydata[0], (list, tuple)):
            if bar_fmt:
                ydata = graph.DataSeries(ydata).toBarY()
            self.data = [zip(xdata, ydata)]
        else:
            self.data = []
            for ys in ydata:
                if bar_fmt:
                    ys = graph.DataSeries(ys).toBarY()
                self.data.append(zip(xdata, ys))

        self.width = width
        self.height = height
        Templated.__init__(self)


class RedditAds(Templated):
    def __init__(self, **kw):
        self.sr_name = c.site.name
        self.adsrs = AdSR.by_sr_merged(c.site)
        self.total = 0

        self.adsrs.sort(key=lambda a: a._thing1.codename)

        seen = {}
        for adsr in self.adsrs:
            seen[adsr._thing1.codename] = True
            self.total += adsr.weight

        self.other_ads = []
        all_ads = Ad._all_ads()
        all_ads.sort(key=lambda a: a.codename)
        for ad in all_ads:
            if ad.codename not in seen:
                self.other_ads.append(ad)

        Templated.__init__(self, **kw)

class PaymentForm(Templated):
    def __init__(self, link, indx, **kw):
        self.countries = pycountry.countries
        self.link = promote.editable_add_props(link)
        self.campaign = self.link.campaigns[indx]
        self.indx = indx
        Templated.__init__(self, **kw)

class Promote_Graph(Templated):
    
    @classmethod
    @memoize('get_market', time = 60)
    def get_market(cls, user_id, start_date, end_date):
        market = {}
        promo_counter = {}
        def callback(link, bid, bid_day, starti, endi, indx):
            for i in xrange(starti, endi):
                if user_id is None or link.author_id == user_id:
                    if (not promote.is_unpaid(link) and 
                        not promote.is_rejected(link)):
                        market[i] = market.get(i, 0) + bid_day
                        promo_counter[i] = promo_counter.get(i, 0) + 1
        cls.promo_iter(start_date, end_date, callback)
        return market, promo_counter

    @classmethod
    def promo_iter(cls, start_date, end_date, callback):
        size = (end_date - start_date).days
        for link, indx, s, e in cls.get_current_promos(start_date, end_date):
            if indx in link.campaigns:
                sdate, edate, bid, sr, trans_id = link.campaigns[indx]
                if isinstance(sdate, datetime.datetime):
                    sdate = sdate.date()
                if isinstance(edate, datetime.datetime):
                    edate = edate.date()
                starti = max((sdate - start_date).days, 0)
                endi   = min((edate - start_date).days, size)
                bid_day = bid / max((edate - sdate).days, 1)
                callback(link, bid, bid_day, starti, endi, indx)

    @classmethod
    def get_current_promos(cls, start_date, end_date):
        # grab promoted links
        # returns a list of (thing_id, campaign_idx, start, end)
        promos = PromotionWeights.get_schedule(start_date, end_date)
        # sort based on the start date
        promos.sort(key = lambda x: x[2])

        # wrap the links
        links = wrap_links([p[0] for p in promos])
        # remove rejected/unpaid promos
        links = dict((l._fullname, l) for l in links.things
                     if promote.is_accepted(l) or promote.is_unapproved(l))
        # filter promos accordingly
        promos = [(links[thing_name], indx, s, e) 
                  for thing_name, indx, s, e in promos
                  if links.has_key(thing_name)]

        return promos

    def __init__(self):
        self.now = promote.promo_datetime_now()

        start_date = promote.promo_datetime_now(offset = -7).date()
        end_date = promote.promo_datetime_now(offset = 7).date()

        size = (end_date - start_date).days

        # these will be cached queries
        market, promo_counter = self.get_market(None, start_date, end_date)
        my_market = market
        if not c.user_is_sponsor:
            my_market = self.get_market(c.user._id, start_date, end_date)[0]

        # determine the range of each link
        promote_blocks = []
        def block_maker(link, bid, bid_day, starti, endi, indx):
            if ((c.user_is_sponsor or link.author_id == c.user._id)
                and not promote.is_rejected(link)
                and not promote.is_unpaid(link)):
                promote_blocks.append( (link, bid, starti, endi, indx) )
        self.promo_iter(start_date, end_date, block_maker)

        # now sort the promoted_blocks into the most contiguous chuncks we can
        sorted_blocks = []
        while promote_blocks:
            cur = promote_blocks.pop(0)
            while True:
                sorted_blocks.append(cur)
                # get the future items (sort will be preserved)
                future = filter(lambda x: x[2] >= cur[3], promote_blocks)
                if future:
                    # resort by date and give precidence to longest promo:
                    cur = min(future, key = lambda x: (x[2], x[2]-x[3]))
                    promote_blocks.remove(cur)
                else:
                    break

        # load recent traffic as well:
        self.recent = {}
        #TODO 
        for k, v in []:#load_summary("thing"):
            if k.startswith('t%d_' % Link._type_id):
                self.recent[k] = v

        if self.recent:
            link_listing = wrap_links(self.recent.keys())
            for t in link_listing:
                self.recent[t._fullname].insert(0, t)

            self.recent = self.recent.values()
            self.recent.sort(key = lambda x: x[0]._date)

        pool =PromotionWeights.bid_history(promote.promo_datetime_now(offset=-30),
                                           promote.promo_datetime_now(offset=2))
        if pool:
            # we want to generate a stacked line graph, so store the
            # bids and the total including refunded amounts
            total_sale = sum((b-r) for (d, b, r) in pool)
            total_refund = sum(r for (d, b, r) in pool)
            
            self.money_graph = TrafficGraph([(d, b, r) for (d, b, r) in pool],
                                            colors = ("008800", "FF0000"),
                                            ylabels = ['total ($)'],
                                            title = ("monthly sales ($%.2f charged, $%.2f credits)" %
                                                     (total_sale, total_refund)),
                                            multiy = False)

            #TODO
            self.top_promoters = []
        else:
            self.money_graph = None
            self.top_promoters = []

        # graphs of impressions and clicks
        self.promo_traffic = load_traffic('day', 'promos')

        impressions = [(d, i) for (d, (i, k)) in self.promo_traffic]
        pool = dict((d, b+r) for (d, b, r) in pool)

        if impressions:
            self.imp_graph = TrafficGraph(impressions, ylabels = ['total'],
                                          title = "impressions")

            clicks = [(d, k) for (d, (i, k)) in self.promo_traffic]

            CPM = [(d, (pool.get(d, 0) * 1000. / i) if i else 0)
                   for (d, (i, k)) in self.promo_traffic if d in pool]

            CPC = [(d, (100 * pool.get(d, 0) / k) if k else 0)
                   for (d, (i, k)) in self.promo_traffic if d in pool]

            CTR = [(d, (100 * float(k) / i if i else 0))
                   for (d, (i, k)) in self.promo_traffic if d in pool]

            self.cli_graph = TrafficGraph(clicks, ylabels = ['total'],
                                          title = "clicks")
            mean_CPM = sum(x[1] for x in CPM) * 1. / max(len(CPM), 1)
            self.cpm_graph = TrafficGraph([(d, min(x, mean_CPM*2)) for d, x in CPM],
                                          colors = ["336699"], ylabels = ['CPM ($)'],
                                          title = "cost per 1k impressions " + 
                                          "($%.2f average)" % mean_CPM)

            mean_CPC = sum(x[1] for x in CPC) * 1. / max(len(CPC), 1)
            self.cpc_graph = TrafficGraph([(d, min(x, mean_CPC*2)) for d, x in CPC],
                                          colors = ["336699"], ylabels = ['CPC ($0.01)'],
                                          title = "cost per click " + 
                                          "($%.2f average)" % (mean_CPC/100.))

            self.ctr_graph = TrafficGraph(CTR, colors = ["336699"], ylabels = ['CTR (%)'],
                                          title = "click through rate")

        else:
            self.imp_graph = self.cli_graph = None
            self.cpc_graph = self.cpm_graph = None
            self.ctr_graph = None

        self.promo_traffic = dict(self.promo_traffic)

        Templated.__init__(self,
                           total_size = size,
                           market = market,
                           my_market = my_market, 
                           promo_counter = promo_counter,
                           start_date = start_date,
                           promote_blocks = sorted_blocks)

    def to_iter(self, localize = True):
        def num(x):
            if localize:
                return locale.format('%d', x, True)
            return str(x)
        for link, uimp, nimp, ucli, ncli in self.recent:
            yield (link._date.strftime("%Y-%m-%d"),
                   num(uimp), num(nimp), num(ucli), num(ncli),
                   num(link._ups - link._downs), 
                   "$%.2f" % link.promote_bid,
                   _force_unicode(link.title))

    def as_csv(self):
        return to_csv(self.to_iter(localize = False))

class InnerToolbarFrame(Templated):
    def __init__(self, link, expanded = False):
        Templated.__init__(self, link = link, expanded = expanded)

class RawString(Templated):
   def __init__(self, s):
       self.s = s

   def render(self, *a, **kw):
       return unsafe(self.s)

class Dart_Ad(CachedTemplate):
    def __init__(self, tag = None):
        tag = tag or "homepage"
        tracker_url = AdframeInfo.gen_url(fullname = "dart_" + tag,
                                          ip = request.ip)
        Templated.__init__(self, tag = tag, tracker_url = tracker_url)

    def render(self, *a, **kw):
        res = CachedTemplate.render(self, *a, **kw)
        return responsive(res, False)

class HouseAd(CachedTemplate):
    def __init__(self, rendering, linkurl, submit_link):
        Templated.__init__(self, rendering=rendering,
                           linkurl = linkurl,
                           submit_link = submit_link)

    def render(self, *a, **kw):
        res = CachedTemplate.render(self, *a, **kw)
        return responsive(res, False)

class ComScore(CachedTemplate):
    pass

def render_ad(reddit_name=None, codename=None):
    if not reddit_name:
        reddit_name = g.default_sr

    if codename:
        if codename == "DART":
            return Dart_Ad(reddit_name).render()
        else:
            try:
                ad = Ad._by_codename(codename)
            except NotFound:
                abort(404)
            attrs = ad.important_attrs()
            return HouseAd(**attrs).render()

    try:
        sr = Subreddit._by_name(reddit_name)
    except NotFound:
        return Dart_Ad(g.default_sr).render()

    ads = {}

    for adsr in AdSR.by_sr_merged(sr):
        ad = adsr._thing1
        ads[ad.codename] = (ad, adsr.weight)

    total_weight = sum(t[1] for t in ads.values())

    if total_weight == 0:
        log_text("no ads", "No ads found for %s" % reddit_name, "error")
        abort(404)

    lotto = random.randint(0, total_weight - 1)
    winner = None
    for t in ads.values():
        lotto -= t[1]
        if lotto <= 0:
            winner = t[0]

            if winner.codename == "DART":
                return Dart_Ad(reddit_name).render()
            else:
                attrs = winner.important_attrs()
                return HouseAd(**attrs).render()

    # No winner?

    log_text("no winner",
             "No winner found for /r/%s, total_weight=%d" %
             (reddit_name, total_weight),
             "error")

    return Dart_Ad(reddit_name).render()
