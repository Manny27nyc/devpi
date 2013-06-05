
import py
from py.xml import html
from devpi_server.types import lazydecorator, cached_property
from bottle import response, request, abort, redirect, HTTPError, auth_basic
from bottle import BaseResponse, HTTPResponse, static_file
import bottle
import json
import itsdangerous
import logging

log = logging.getLogger(__name__)

LOGINCOOKIE = "devpi-login"
MAXDOCZIPSIZE = 30 * 1024 * 1024    # 30MB


def simple_html_body(title, bodytags, extrahead=""):
    return html.html(
        html.head(
            html.title(title),
            extrahead,
        ),
        html.body(
            html.h1(title),
            bodytags
        )
    )

#def abort_json(code, body):
#    d = dict(error=body)
#    raise HTTPResponse(body=json.dumps(d, indent=2)+"\n",
#                       status=code, headers=
#                       {"content-type": "application/json"})

def abort(code, body):
    if "application/json" in request.headers.get("Accept", ""):
        apireturn(code, body)
    bottle.abort(code, body)

def abort_authenticate():
    err = HTTPError(401, "authentication required")
    err.add_header('WWW-Authenticate', 'Basic realm="pypi"')
    raise err

def apireturn(code, message=None, resource=None):
    d = dict(status=code)
    if resource is not None:
        d["resource"] = resource
    if message:
        d["message"] = message
    data = json.dumps(d, indent=2) + "\n"
    raise HTTPResponse(body=data, status=code, header=
                    {"content-type": "application/json"})

route = lazydecorator()

class PyPIView:
    LOGIN_EXPIRATION = 60*60*10  # 10 hours

    def __init__(self, xom):
        self.xom = xom
        self.db = xom.db


    #
    # support functions
    #

    @cached_property
    def signer(self):
        return itsdangerous.TimestampSigner(self.xom.config.secret)

    def require_user(self, user):
        #log.debug("headers %r", request.headers.items())
        try:
            authuser, authpassword = request.auth
        except TypeError:
            log.warn("could not read auth header")
            abort_authenticate()
        log.debug("detected auth for user %r", authuser)
        try:
            val = self.signer.unsign(authpassword, self.LOGIN_EXPIRATION)
        except itsdangerous.BadData:
            if self.db.user_validate(authuser, authpassword):
                return
            log.warn("invalid authentication for user %r", authuser)
            abort_authenticate()
        if not val.startswith(authuser + "-"):
            log.warn("mismatch credential for user %r", authuser)
            abort_authenticate()
        if authuser == "root" or authuser == user:
            return
        log.warn("user %r not authorized, requiring %r", authuser, user)
        abort_authenticate()

    def set_user(self, user, hash):
        pseudopass = self.signer.sign(user + "-" + hash)
        return {"password":  pseudopass,
                "expiration": self.LOGIN_EXPIRATION}

    def getstage(self, user, index):
        stage = self.db.getstage(user, index)
        if not stage:
            abort(404, "no such stage")
        return stage

    #
    # index serving and upload
    #
    @route("/ext/pypi<rest:re:.*>")
    def extpypi_redirect(self, rest):
        redirect("/root/pypi%s" % rest)

    @route("/<user>/<index>/simple/<projectname>")
    @route("/<user>/<index>/simple/<projectname>/")
    def simple_list_project(self, user, index, projectname):
        # we only serve absolute links so we don't care about the route's slash
        stage = self.getstage(user, index)
        result = stage.getreleaselinks(projectname)
        if isinstance(result, int):
            if result == 404:
                abort(404, "no such project")
            if result >= 500:
                abort(502, "upstream server has internal error")
            if result < 0:
                abort(502, "upstream server not reachable")

        links = []
        for entry in result:
            href = "/" + entry.relpath
            if entry.eggfragment:
                href += "#egg=%s" % entry.eggfragment
            elif entry.md5:
                href += "#md5=%s" % entry.md5
            links.append((href, entry.basename))

        # construct html
        body = []
        for entry in links:
            body.append(html.a(entry[1], href=entry[0]))
            body.append(html.br())
        return simple_html_body("%s: links for %s" % (stage.name, projectname),
                                body).unicode()

    @route("/<user>/<index>/f/<relpath:re:.*>")
    def pkgserv(self, user, index, relpath):
        relpath = request.path.strip("/")
        filestore = self.xom.releasefilestore
        headers, itercontent = filestore.iterfile(relpath, self.xom.httpget)
        response.content_type = headers["content-type"]
        if "content-length" in headers:
            response.content_length = headers["content-length"]
        for x in itercontent:
            yield x

    @route("/<user>/<index>/simple/")
    def simple_list_all(self, user, index):
        stage = self.getstage(user, index)
        names = stage.getprojectnames()
        body = []
        for name in names:
            body.append(html.a(name, href=name + "/"))
            body.append(html.br())
        return simple_html_body("%s: list of accessed projects" % stage.name,
                                body).unicode()
    @route("/<user>/<index>/", method="GET")
    def indexroot(self, user, index):
        stage = self.getstage(user, index)
        bases = html.ul()
        for base in stage.ixconfig["bases"]:
            bases.append(html.li(
                html.a("%s" % base, href="/%s/" % base),
                " (",
                html.a("simple", href="/%s/simple/" % base),
                " )",
            ))
        if bases:
            bases = [html.h2("inherited bases"), bases]

        return simple_html_body("%s index" % stage.name, [
            html.ul(
                html.li(html.a("simple index", href="simple/")),
            ),
            bases,
        ]).unicode()

    @route("/<user>/<index>", method=["PUT", "PATCH"])
    def index_create_or_modify(self, user, index):
        ixconfig = self.db.user_indexconfig_get(user, index)
        if request.method == "PUT" and ixconfig is not None:
            apireturn(409, "index %s/%s exists" % (user, index))
        kvdict = getkvdict_index(getjson())
        kvdict.setdefault("type", "stage")
        kvdict.setdefault("bases", ["root/dev"])
        kvdict.setdefault("volatile", True)
        ixconfig = self.db.user_indexconfig_set(user, index, **kvdict)
        apireturn(201, resource=ixconfig)

    @route("/<user>/<index>", method=["DELETE"])
    def index_delete(self, user, index):
        indexname = user + "/" + index
        if not self.db.user_indexconfig_delete(user, index):
            apireturn(404, "index %s does not exist" % indexname)
        apireturn(201, "index %s deleted" % indexname)

    @route("/<user>/", method="GET")
    def index_list(self, user):
        userconfig = self.db.user_get(user)
        if not userconfig:
            apireturn(404, "user %s does not exist" % user)
        indexes = {}
        userindexes = userconfig.get("indexes", {})
        for name, val in userindexes.items():
            indexes["%s/%s" % (user, name)] = val
        apireturn(200, resource=indexes)

    @route("/<user>/<index>/", method="POST")
    @route("/<user>/<index>/pypi", method="POST")
    @route("/<user>/<index>/pypi/", method="POST")
    def submit(self, user, index):
        self.require_user(user)
        try:
            action = request.forms[":action"]
        except KeyError:
            abort(400, ":action field not found")
        log.debug("received POST action %r" %(action))
        stage = self.getstage(user, index)
        if action == "submit":
            return self._register_metadata(stage, request.forms)
        elif action in ("doc_upload", "file_upload"):
            try:
                content = request.files["content"]
            except KeyError:
                abort(400, "content file field not found")
            name = request.forms.get("name")
            version = request.forms.get("version", "")
            if not stage.get_metadata(name, version):
                self._register_metadata(stage, request.forms)
            if action == "file_upload":
                stage.store_releasefile(content.filename, content.value)
            else:
                if len(content.value) > MAXDOCZIPSIZE:
                    abort(413, "zipfile too large")
                stage.store_doczip(name, content.value)
        else:
            abort(400, "action %r not supported" % action)
        return ""

    def _register_metadata(self, stage, form):
        metadata = {}
        for key in stage.metadata_keys:
            metadata[key] = form.get(key, "")
        log.info("got submit release info %r", metadata["name"])
        stage.register_metadata(metadata)

    @route("/<user>/<index>/pypi/<name>/<version>/", method="GET")
    @route("/<user>/<index>/pypi/<name>/<version>", method="GET")
    def versioned_description(self, user, index, name, version):
        stage = self.getstage(user, index)
        content = stage.get_description(name, version)
        css = "https://pypi.python.org/styles/styles.css"
        return simple_html_body("%s-%s description" % (name, version),
            py.xml.raw(content), extrahead=
            [html.link(media="screen", type="text/css",
                rel="stylesheet", title="text",
                href="https://pypi.python.org/styles/styles.css")]).unicode()

    @route("/<user>/<index>/pypi/<name>", method="GET")
    @route("/<user>/<index>/pypi/<name>/", method="GET")
    def versions_of_descriptions(self, user, index, name):
        stage = self.getstage(user, index)
        descriptions = stage.get_description_versions(name)
        l = []
        for desc in descriptions:
            l.append(
                html.a(desc, href="/%s/%s/pypi/%s/%s/" % (
                       user, index, name, desc))
            )
            l.append(html.br())
        body = simple_html_body("Extracted Descriptions for %r" % name, l)
        return body.unicode()

    @route("/<user>/<index>/")
    def indexroot(self, user, index):
        stage = self.getstage(user, index)
        bases = html.ul()
        for base in stage.ixconfig["bases"]:
            bases.append(html.li(
                html.a("%s" % base, href="/%s/" % base),
                " (",
                html.a("simple", href="/%s/simple/" % base),
                " )",
            ))
        if bases:
            bases = [html.h2("inherited bases"), bases]

        return simple_html_body("%s index" % stage.name, [
            html.ul(
                html.li(html.a("simple index", href="simple/")),
            ),
            bases,
        ]).unicode()


    # showing uploaded package documentation
    @route("/<user>/<index>/+doc/<name>/<relpath:re:.*>",
           method="GET")
    def doc_show(self, user, index, name, relpath):
        if not relpath:
            redirect("index.html")
        key = self.db.keyfs.STAGEDOCS(user=user, index=index, name=name)
        if not key.filepath.check():
            abort(404, "no documentation available")
        return static_file(relpath, root=str(key.filepath))

    #
    # supplying basic API locations for all services
    #

    @route("/<user>/<index>/-api")
    @route("/<user>/<index>/pypi/-api")
    @route("/<user>/<index>/simple/-api")
    def apiconfig(self, user, index):
        if not self.db.user_indexconfig_get(user, index):
            abort(404, "index %s/%s does not exist" %(user, index))
        root = "/"
        apidict = {
            "resultlog": "/resultlog",
            "login": "/login",
            "pypisubmit": "/%s/%s/pypi" % (user, index),
            "pushrelease": "/%s/%s/push" % (user, index),
            "simpleindex": "/%s/%s/simple/" % (user, index),
        }
        apireturn(200, resource=apidict)

    #
    # login and user handling
    #
    @route("/login", method="POST")
    def login(self):
        dict = getjson()
        user = dict.get("user", None)
        password = dict.get("password", None)
        if user is None or password is None:
            abort(400, "Bad request: no user/password specified")
        hash = self.db.user_validate(user, password)
        if hash:
            return self.set_user(user, hash)
        apireturn(401, "user %r could not be authenticated" % user)

    @route("/<user>", method="PATCH")
    @route("/<user>/", method="PATCH")
    def user_patch(self, user):
        self.require_user(user)
        dict = getjson()
        if "password" in dict:
            hash = self.db.user_setpassword(user, dict["password"])
            return self.set_user(user, hash)
        apireturn(400, "could not decode request")

    @route("/<user>", method="PUT")
    def user_create(self, user):
        if self.db.user_exists(user):
            apireturn(409, "user already exists")
        kvdict = getjson()
        if "password" in kvdict and "email" in kvdict:
            hash = self.db.user_create(user, **kvdict)
            apireturn(201, resource=self.db.user_get(user))
        apireturn(400, "password and email values need to be set")

    @route("/<user>", method="DELETE")
    def user_delete(self, user):
        self.require_user(user)
        if not self.db.user_exists(user):
            apireturn(404, "user %r does not exist" % user)
        self.db.user_delete(user)
        apireturn(200, "user %r deleted" % user)

    @route("/", method="GET")
    def user_list(self):
        #accept = request.headers.get("accept")
        #if accept is not None:
        #    if accept.endswith("/json"):
        d = {}
        for user in self.db.user_list():
            d[user] = self.db.user_get(user)
        apireturn(200, resource=d)

def getjson():
    dict = request.json
    if dict is None:
        try:
            return json.load(request.body)
        except ValueError:
            abort(400, "Bad request: could not decode")
    return dict


def getkvdict_index(req):
    req_volatile = req.get("volatile")
    kvdict = dict(volatile=True, type="stage", bases=["root/dev", "root/pypi"])
    if req_volatile is not None:
        if req_volatile == False or req_volatile.lower() in ["false", "no"]:
            kvdict["volatile"] = False
    bases = req.get("bases")
    if bases is not None and not isinstance(bases, list):
        kvdict["bases"] = bases.split(",")
    if "type" in req:
        kvdict["type"] = req["type"]
    return kvdict