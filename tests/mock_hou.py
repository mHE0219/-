"""A tiny stand-in for Houdini's `hou` module — just enough of the API used by
houdini/python/geobox_houdini to run its code paths outside Houdini.
It checks logic and API usage shape, NOT real Houdini behaviour."""
import re
import sys
import types


class _AT:
    Point, Prim, Global, Vertex = "point", "prim", "global", "vertex"


attribType = _AT


class NodeError(Exception):
    pass


class OperationFailed(Exception):
    pass


class Matrix4:
    def __init__(self, rows=None):
        self.rows = [list(r) for r in rows] if rows else [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


class _BBox:
    def __init__(self, mn, mx):
        self._mn, self._mx = mn, mx

    def minvec(self):
        return list(self._mn)

    def maxvec(self):
        return list(self._mx)

    def sizevec(self):
        return [self._mx[i] - self._mn[i] for i in range(3)]


class hmath:  # noqa: N801
    @staticmethod
    def buildTranslate(x, y, z):
        return Matrix4([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [x, y, z, 1]])

    @staticmethod
    def buildRotate(rx, ry, rz):
        import math
        a = math.radians(ry)
        c, s = math.cos(a), math.sin(a)
        return Matrix4([[c, 0, -s, 0], [0, 1, 0, 0], [s, 0, c, 0], [0, 0, 0, 1]])


class Point:
    def __init__(self, geo, idx):
        self.geo, self.idx, self.pos, self.attrs = geo, idx, (0.0, 0.0, 0.0), {}

    def number(self):
        return self.idx

    def position(self):
        return self.pos

    def setPosition(self, p):
        assert len(p) == 3
        self.pos = tuple(float(v) for v in p)

    def attribValue(self, name):
        if name not in self.geo.pattr:
            raise OperationFailed("no point attrib " + name)
        return self.attrs.get(name, self.geo.pattr[name])

    def setAttribValue(self, name, v):
        assert name in self.geo.pattr, name
        self.attrs[name] = v


class Prim:
    def __init__(self, geo, closed=True):
        self.geo, self.verts, self.attrs, self.closed = geo, [], {}, closed

    def addVertex(self, pt):
        assert isinstance(pt, Point)
        self.verts.append(pt)

    def vertices(self):
        return tuple(types.SimpleNamespace(point=(lambda p=p: p)) for p in self.verts)

    def setAttribValue(self, name, v):
        assert name in self.geo.prattr, "prim attrib %s not added" % name
        d = self.geo.prattr[name]
        if isinstance(d, tuple):
            assert len(tuple(v)) == len(d), (name, v, d)
        self.attrs[name] = v

    def attribValue(self, name):
        if name not in self.geo.prattr:
            raise OperationFailed("no prim attrib " + name)
        return self.attrs.get(name, self.geo.prattr[name])


class Group:
    def __init__(self, name):
        self.name, self.items = name, []

    def add(self, x):
        self.items.append(x)


class Geometry:
    def __init__(self):
        self._pts, self._prims = [], []
        self.pattr, self.prattr, self.gattr, self.gval = {}, {}, {}, {}
        self.groups = {}

    # attributes
    def findPointAttrib(self, n):
        return n if n in self.pattr else None

    def findPrimAttrib(self, n):
        return n if n in self.prattr else None

    def findGlobalAttrib(self, n):
        return n if n in self.gattr else None

    def addAttrib(self, kind, name, default):
        {"point": self.pattr, "prim": self.prattr, "global": self.gattr}[kind][name] = default
        return name

    def setGlobalAttribValue(self, n, v):
        assert n in self.gattr
        self.gval[n] = v

    def _bulk(self, name, values, conv):
        d = self.pattr[name]
        size = len(d) if isinstance(d, tuple) else 1
        assert len(values) == size * len(self._pts), (name, len(values), size, len(self._pts))
        for i, p in enumerate(self._pts):
            chunk = values[i * size:(i + 1) * size]
            p.attrs[name] = tuple(conv(v) for v in chunk) if size > 1 else conv(chunk[0])

    def setPointFloatAttribValues(self, n, v):
        self._bulk(n, list(v), float)

    def setPointIntAttribValues(self, n, v):
        self._bulk(n, list(v), int)

    def setPointStringAttribValues(self, n, v):
        self._bulk(n, list(v), str)

    def setPrimStringAttribValues(self, n, v):
        assert len(v) == len(self._prims)
        for p, x in zip(self._prims, v):
            p.attrs[n] = x

    # topology
    def createPoint(self):
        p = Point(self, len(self._pts))
        self._pts.append(p)
        return p

    def createPoints(self, positions):
        out = []
        for pos in positions:
            p = self.createPoint()
            p.setPosition(pos)
            out.append(p)
        return tuple(out)

    def createPolygon(self, is_closed=True):
        pr = Prim(self, is_closed)
        self._prims.append(pr)
        return pr

    def points(self):
        return tuple(self._pts)

    def prims(self):
        return tuple(self._prims)

    def createPointGroup(self, name):
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name), name
        g = self.groups[name] = Group(name)
        return g

    def merge(self, other):
        off = len(self._pts)
        for k, v in other.pattr.items():
            self.pattr.setdefault(k, v)
        for k, v in other.prattr.items():
            self.prattr.setdefault(k, v)
        for p in other._pts:
            q = self.createPoint()
            q.pos, q.attrs = p.pos, dict(p.attrs)
        for pr in other._prims:
            n = self.createPolygon(pr.closed)
            n.verts = [self._pts[off + v.idx] for v in pr.verts]
            n.attrs = dict(pr.attrs)

    def transform(self, m):
        r = m.rows
        for p in self._pts:
            x, y, z = p.pos
            p.pos = tuple(x * r[0][j] + y * r[1][j] + z * r[2][j] + r[3][j] for j in range(3))

    def boundingBox(self):
        xs = [p.pos for p in self._pts] or [(0, 0, 0)]
        return _BBox([min(v[i] for v in xs) for i in range(3)], [max(v[i] for v in xs) for i in range(3)])

    def freeze(self):
        g = Geometry()
        g.merge(self)
        g.gattr, g.gval = dict(self.gattr), dict(self.gval)
        return g

    def clear(self):
        self.__init__()

    def attribValue(self, name):  # detail attribute
        if name not in self.gattr:
            raise OperationFailed("no detail attrib " + name)
        return self.gval.get(name, self.gattr[name])

    def loadFromFile(self, path):
        if not path.lower().endswith(".obj"):
            raise OperationFailed("mock can only read .obj: " + path)
        verts = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("v "):
                    verts.append(self.createPoints([tuple(float(t) for t in line.split()[1:4])])[0])
                elif line.startswith("f "):
                    pr = self.createPolygon()
                    for tok in line.split()[1:]:
                        pr.addVertex(verts[int(tok.split("/")[0]) - 1])

    def saveToFile(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# mock geo\n")


class _PT:
    def __init__(self, kind):
        self._k = kind

    def type(self):
        return self._k


class Parm:
    def __init__(self, node, name, value=""):
        self.node, self._name, self.value, self.expr = node, name, value, None

    def name(self):
        return self._name

    def parmTemplate(self):
        return _PT("Toggle" if isinstance(self.value, int) and not isinstance(self.value, bool) else "String")

    def set(self, v):
        self.value = v

    def setExpression(self, e):
        self.expr = e

    def eval(self):
        return self.value

    def evalAsString(self):
        return str(self.value)


class _Type:
    def __init__(self, name):
        self._n = name

    def name(self):
        return self._n


class _PTG:
    def __init__(self):
        self.items = []

    def find(self, name):
        return None

    def remove(self, name):
        pass

    def append(self, t):
        self.items.append(t)


class _Template:
    def __init__(self, name, *a, **k):
        self.name, self.children = name, []
        dv = k.get("default_value")
        self.default = dv[0] if isinstance(dv, tuple) else dv

    def addParmTemplate(self, t):
        self.children.append(t)


class Node:
    STRICT_PARMS = None  # set of parm names that exist when strict (None = permissive)

    def __init__(self, path="/obj/n", type_name="geo", parent=None):
        self._path, self._type, self._parent = path, type_name, parent
        self._parms, self._children, self._geo, self._inputs = {}, {}, Geometry(), {}
        self.flags = {}

    def path(self):
        return self._path

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def type(self):
        return _Type(self._type)

    def parent(self):
        return self._parent

    def geometry(self):
        return self._geo

    def parm(self, name):
        if name not in self._parms:
            self._parms[name] = Parm(self, name)
        return self._parms[name]

    def parmTuple(self, name):
        return self.parm(name)

    def evalParm(self, name):
        return self.parm(name).eval()

    def node(self, rel):
        return self._children.get(rel)

    def children(self):
        return tuple(self._children.values())

    def createNode(self, type_name, name=None):
        name = name or "%s1" % type_name
        n = Node(self._path + "/" + name, type_name, self)
        self._children[name] = n
        return n

    def destroy(self):
        if self._parent:
            self._parent._children.pop(self.name(), None)

    def setInput(self, i, n):
        if n is None:
            self._inputs.pop(i, None)
        else:
            self._inputs[i] = n

    def inputs(self):
        if not self._inputs:
            return ()
        return tuple(self._inputs.get(i) for i in range(max(self._inputs) + 1))

    def errors(self):
        return tuple(getattr(self, "_errors", ()))

    def warnings(self):
        return ()

    def parms(self):
        return tuple(self._parms.values())

    def parmTemplateGroup(self):
        return _PTG()

    def setParmTemplateGroup(self, g):
        for t in g.items:
            for c in getattr(t, "children", []):
                self.parm(c.name).set(c.default if c.default is not None else "")

    def cook(self, force=False):
        """Evaluate a tiny SOP subset so whole networks can run: python (exec its code with
        pwd = this node, geometry starts as a copy of input 0), merge, switch (index from
        a ch("../parm") expression), box, copytopoints (one tri per matched point), others pass
        input 0 through. Errors are recorded (node.errors()) and re-raised like hou."""
        self.cooked = True
        self._errors = []
        ins = self.inputs()
        for n in ins:
            if n is not None:
                n.cook()
        t = self._type.split("::")[0]
        try:
            if t == "python":
                g = Geometry()
                if ins and ins[0] is not None:
                    g.merge(ins[0]._geo)
                self._geo = g
                prev = _PWD[0]
                _PWD[0] = self
                try:
                    exec(self.parm("python").value, {"__name__": "python_sop"})  # noqa: S102
                finally:
                    _PWD[0] = prev
            elif t == "merge":
                g = Geometry()
                for n in ins:
                    if n is not None:
                        g.merge(n._geo)
                self._geo = g
            elif t == "switch":
                p = self.parm("input")
                m = re.match(r'ch\("\.\./(\w+)"\)', p.expr or "")
                idx = int(self._parent.parm(m.group(1)).value or 0) if m else int(p.value or 0)
                src = ins[idx] if idx < len(ins) else None
                self._geo = src._geo.freeze() if src is not None else Geometry()
            elif t == "box":
                g = Geometry()
                v = _Verb("box")
                v.setParms({"size": tuple(self.parm("size").value or (1, 1, 1)), "t": tuple(self.parm("t").value or (0, 0, 0))})
                v.execute(g, [])
                self._geo = g
            elif t == "copytopoints":
                g = Geometry()
                src, tgt = (ins + (None, None))[:2]
                names = {pr.attrs.get("name") for pr in src._geo.prims()} if src is not None else set()
                g.addAttrib("prim", "name", "")
                for pt in (tgt._geo.points() if tgt is not None else ()):
                    nm = pt.attrs.get("name")
                    if nm in names:
                        x, y, z = pt.pos
                        a, b, c = g.createPoints([(x, y, z), (x + 1, y, z), (x, y + 1, z)])
                        pr = g.createPolygon()
                        for q in (a, b, c):
                            pr.addVertex(q)
                        pr.attrs["name"] = nm
                self._geo = g
            elif t in ("geo", "network", "subnet", "topnet", "sublayer"):
                pass
            else:
                self._geo = ins[0]._geo.freeze() if ins and ins[0] is not None else Geometry()
        except Exception as exc:
            self._errors.append("%s: %s" % (type(exc).__name__, exc))
            raise OperationFailed("%s failed: %s" % (self._path, exc)) from exc

    def __getattr__(self, item):  # setDisplayFlag, layoutChildren, setComment, ...
        if item.startswith("__"):
            raise AttributeError(item)
        return lambda *a, **k: None


_PWD = [None]


def pwd():
    return _PWD[0]


def set_pwd(n):
    _PWD[0] = n


_NODES = {}


def node(path):
    return _NODES.setdefault(path, Node(path, "network"))


def expandString(s):
    return s


def isUIAvailable():
    return False


class _Verb:
    def __init__(self, name):
        self.name, self.parms = name, {}

    def setParms(self, d):
        self.parms.update(d)

    def execute(self, geo, inputs):
        sx, sy, sz = self.parms.get("size", (1, 1, 1))
        tx, ty, tz = self.parms.get("t", (0, 0, 0))
        pts = geo.createPoints([(tx + sx * (i - .5), ty + sy * (j - .5), tz + sz * (k - .5)) for i in (0, 1) for j in (0, 1) for k in (0, 1)])
        for f in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            pr = geo.createPolygon()
            for i in f:
                pr.addVertex(pts[i])


class _Cat:
    def nodeVerb(self, name):
        return _Verb(name)


def sopNodeTypeCategory():
    return _Cat()


class _Enum:
    def __getattr__(self, k):
        return k


stringParmType = fileType = folderType = scriptLanguage = nodeFlag = parmTemplateType = _Enum()
FolderParmTemplate = StringParmTemplate = ButtonParmTemplate = ToggleParmTemplate = MenuParmTemplate = _Template
IntParmTemplate = FloatParmTemplate = _Template


def applicationVersionString():
    return "mock-0.0"


def licenseCategory():
    return "mock"


def selectedNodes():
    return ()


def getenv(name, default=None):
    import os
    return os.environ.get(name, default)


def findFile(rel):
    import os
    root = os.environ.get("GEOBOX")
    p = os.path.join(root or "", "houdini", rel)
    if root and os.path.exists(p):
        return p
    raise OperationFailed("file not found on HOUDINI_PATH: " + rel)


class _HipFile:
    saved = None

    def save(self, file_name=None):
        with open(file_name, "w", encoding="utf-8") as fh:
            fh.write("mock hip\n")
        _HipFile.saved = file_name

    def clear(self, suppress_save_prompt=False):
        pass


hipFile = _HipFile()


def install():
    mod = sys.modules[__name__]
    sys.modules["hou"] = mod
    return mod
