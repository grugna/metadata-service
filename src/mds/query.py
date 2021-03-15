import json

from fastapi import HTTPException, Query, APIRouter
from sqlalchemy import cast, column, exists
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.operators import ColumnOperators
from starlette.requests import Request
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from parsimonious.grammar import Grammar
from parsimonious.nodes import NodeVisitor
from parsimonious.exceptions import IncompleteParseError, ParseError, VisitationError

from .models import db, Metadata

mod = APIRouter()


@mod.get("/metadata")
async def search_metadata(
    request: Request,
    data: bool = Query(
        False,
        description="Switch to returning a list of GUIDs (false), "
        "or GUIDs mapping to their metadata (true).",
    ),
    limit: int = Query(
        10, description="Maximum number of records returned. (max: 2000)"
    ),
    offset: int = Query(0, description="Return results at this given offset."),
):
    """Search the metadata.

    Without filters, this will return all data. Add filters as query strings like this:

        GET /metadata?a=1&b=2

    This will match all records that have metadata containing all of:

        {"a": 1, "b": 2}

    The values are always treated as strings for filtering. Nesting is supported:

        GET /metadata?a.b.c=3

    Matching records containing:

        {"a": {"b": {"c": 3}}}

    Providing the same key with more than one value filters records whose value of the
    given key matches any of the given values. But values of different keys must all
    match. For example:

        GET /metadata?a.b.c=3&a.b.c=33&a.b.d=4

    Matches these:

        {"a": {"b": {"c": 3, "d": 4}}}
        {"a": {"b": {"c": 33, "d": 4}}}
        {"a": {"b": {"c": "3", "d": 4, "e": 5}}}

    But won't match these:

        {"a": {"b": {"c": 3}}}
        {"a": {"b": {"c": 3, "d": 5}}}
        {"a": {"b": {"d": 5}}}
        {"a": {"b": {"c": "333", "d": 4}}}

    """
    limit = min(limit, 2000)
    queries = {}
    for key, value in request.query_params.multi_items():
        if key not in {"data", "limit", "offset"}:
            queries.setdefault(key, []).append(value)

    def add_filter(query):
        for path, values in queries.items():
            query = query.where(
                db.or_(Metadata.data[list(path.split("."))].astext == v for v in values)
            )
        return query.offset(offset).limit(limit)

    if data:
        return {
            metadata.guid: metadata.data
            for metadata in await add_filter(Metadata.query).gino.all()
        }
    else:
        return [
            row[0]
            for row in await add_filter(db.select([Metadata.guid]))
            .gino.return_model(False)
            .all()
        ]


#  XXX move long docstring to objects.py
async def search_metadata_helper(
    data: bool = False,
    page: int = 0,
    limit: int = 10,
    filter: str = "",
):
    """
    Helper to search for metadata objects based on filters provided in filter
    param.

    The filtering functionality was primarily driven by the requirement that a
    user be able to get all objects having an authz resource matching a
    user-supplied pattern at any index in the "_resource_paths" array. For
    example, given the following metadata objects:

    {
        "0": {
            "message": "hello",
            "_uploader_id": "100",
            "_resource_paths": [
                "/programs/a",
                "/programs/b"
            ],
            "pet": "dog"
        },
        "1": {
            "message": "greetings",
            "_uploader_id": "101",
            "_resource_paths": [
                "/open",
                "/programs/c/projects/a"
            ],
            "pet": "ferret",
            "sport": "soccer"
        },
        "2": {
            "message": "morning",
            "_uploader_id": "102",
            "_resource_paths": [
                "/programs/d",
                "/programs/e"
            ],
            "counts": [42, 42, 42],
            "pet": "ferret",
            "sport": "soccer"
        },
        "3": {
            "message": "evening",
            "_uploader_id": "103",
            "_resource_paths": [
                "/programs/f/projects/a",
                "/admin"
            ],
            "counts": [1, 3, 5],
            "pet": "ferret",
            "sport": "basketball"
        }
    }

    how do we design a filtering interface that allows the user to get all
    objects having an authz string matching the pattern
    "/programs/%/projects/%" at any index in its "_resource_paths" array? (%
    has been used as the wildcard so far because that's what Postgres uses as
    the wildcard for LIKE) In this case, the "1" and "3" objects should be
    returned.

    The filter syntax that was arrived at ending up following the syntax
    specified by a Node JS implementation
    (https://www.npmjs.com/package/json-api#filtering) of the JSON:API
    specification (https://jsonapi.org/).

    The format for this syntax is filter=(field_name,operator,value), in which
    the field_name is a json key without quotes, operator is one of :eq, :ne,
    :gt, :gte, :lt, :lte, :like, :all, :any (see operators dict), and value is
    a typed json value against which the operator is run.

    Examples:

        - GET /objects?filter=(message,:eq,"morning") returns "2"
        - GET /objects?filter=(counts.1,:eq,3) returns "3"

    Compound expressions are supported:

        - GET /objects?filter=(_resource_paths,:any,(,:like,"/programs/%/projects/%"))
          returns "1" and "3"
        - GET /objects?filter=(counts,:all,(,:eq,42))
          returns "2"

    Boolean expressions are also supported:

        - GET /objects?filter=(or,(_uploader_id,:eq,"101"),(_uploader_id,:eq,"102"))
          returns "1" and "2"
        - GET /objects?filter=(or,(and,(pet,:eq,"ferret"),(sport,:eq,"soccer")),(message,:eq,"hello"))
          returns "0", "1", and "2"

    """
    #  XXX change to 1024
    limit = min(limit, 2000)

    def add_filter(target_key, operator_name, right_operand):
        """
        Wrapper around operators dict. Decides if a scalar or compound operator
        needs to be instantiated based on right_operand.

        Args:
        target_key (str): the json key which we are filtering based on

        operator_name (str): the name of the operator (leading colon included)

        right_operand (str, bool, int, float, dict): With a scalar filter (i.e.
        when it's not a dict), the value that the target value is compared
        against.  With a compound filter (i.e. when it's a dict), the nested
        filter that's run on the target key.

        e.g. for the following requests, the arguments to add_filter:
            * `GET /objects?filter=(_uploader_id,:eq,"57")`
                - target_key is '_uploader_id'
                - operator_name is ':eq'
                - right_operand is '57'
            * `GET /objects?filter=(count,:gte,16)`
                - target_key is 'count'
                - operator_name is ':gte'
                - right_operand is 16 (note this is a python int)
            * `GET /objects?filter=(_resource_paths,:any,(,:like,"/programs/foo/%"))`
                - target_key is '_resource_paths'
                - operator_name is ':any'
                - right_operand is {'name': '', 'op': ':like', 'val': '/programs/foo/%'}

        Returns:
            SQLAlchemy object
        """

        json_object = Metadata.data[list(target_key.split("."))]

        if type(right_operand) is dict:
            #  since right_operand isn't a primitive type, it means that this is a compound
            #  operator
            #  (e.g. GET objects?filter=(_resource_paths,:any,(,:like,"/programs/%")) )
            return operators[operator_name]["sql_clause"](
                json_object, right_operand["op"], right_operand["val"]
            )

        if (
            "type" in operators[operator_name]
            and operators[operator_name]["type"] == "text"
        ):
            json_object = json_object.astext
        else:
            #  this is necessary to differentiate between strings, booleans, numbers,
            #  etc. in JSON
            right_operand = cast(right_operand, JSONB)

        return operators[operator_name]["sql_comparator"](json_object, right_operand)

    def get_any_sql_clause(target_json_key, scalar_operator_name, scalar_right_operand):
        """
        Generate a SQLAlchemy clause that corresponds to the :any operation.

        Args:
            target_json_key (str): the json key pointing to the array which we
            are filtering based on

            scalar_operator_name (str): the name of the operator (leading colon
            included) (see operators dict for a list of valid operators)

            scalar_right_operand (str, bool, int, float, dict): the value that
            each element in the target_json_key array is compared against

        Returns:
            SQLAlchemy clause corresponding to the :any operation.

        Note: get_any_sql_clause could possibly be used for a :has_key
        operation w/ a little more work (e.g. does teams object {} have "bears"
        key: (teams,:any,(,:eq,"bears"))) (would need to use jsonb_object_keys
        https://www.postgresql.org/docs/9.5/functions-json.html#FUNCTIONS-JSON-OP-TABLE)
        """
        if (
            "type" in operators[scalar_operator_name]
            and operators[scalar_operator_name]["type"] == "text"
        ):
            f = db.func.jsonb_array_elements_text
        else:
            f = db.func.jsonb_array_elements
            scalar_right_operand = cast(scalar_right_operand, JSONB)

        f = f(target_json_key).alias()

        return exists(
            db.select("*")
            .select_from(f)
            .where(
                operators[scalar_operator_name]["sql_comparator"](
                    column(f.name), scalar_right_operand
                )
            )
        )

    def get_all_sql_clause(target_json_key, scalar_operator_name, scalar_right_operand):
        """
        Generate a SQLAlchemy clause that corresponds to the :all operation.

        Args:
            target_json_key (str): the json key pointing to the array which we
            are filtering based on

            scalar_operator_name (str): the name of the operator (leading colon
            included) (see operators dict for a list of valid operators)

            scalar_right_operand (str, bool, int, float, dict): the value that
            each element in the target_json_key array is compared against

        Returns:
            SQLAlchemy clause corresponding to the :all operation.
        """
        if (
            "type" in operators[scalar_operator_name]
            and operators[scalar_operator_name]["type"] == "text"
        ):
            f = db.func.jsonb_array_elements_text
        else:
            f = db.func.jsonb_array_elements
            scalar_right_operand = cast(scalar_right_operand, JSONB)

        f = f(target_json_key).alias()
        count_f = db.func.jsonb_array_length(target_json_key)

        return ColumnOperators.__eq__(
            count_f,
            db.select([db.func.count()])
            .select_from(f)
            .where(
                operators[scalar_operator_name]["sql_comparator"](
                    column(f.name), scalar_right_operand
                )
            ),
        )

    operators = {
        ":eq": {"sql_comparator": ColumnOperators.__eq__},
        ":ne": {"sql_comparator": ColumnOperators.__ne__},
        ":gt": {"sql_comparator": ColumnOperators.__gt__},
        ":gte": {"sql_comparator": ColumnOperators.__ge__},
        ":lt": {"sql_comparator": ColumnOperators.__lt__},
        ":lte": {"sql_comparator": ColumnOperators.__le__},
        ":like": {
            "type": "text",
            "sql_comparator": ColumnOperators.like,
        },
        ":all": {"sql_clause": get_all_sql_clause},
        ":any": {"sql_clause": get_any_sql_clause},
    }

    def parse_filter(filter_string):
        """
        Parse filter_string and return an abstract syntax tree representation.

        Args:
            filter_string (str): the filter string to parse
            (e.g. '(_uploader_id,:eq,"57")' )

        Returns:
            A parsimonious abstract syntax tree representation of
            filter_string.
        """
        if not filter_string:
            return

        #  json_value and below was taken from
        #  https://github.com/erikrose/parsimonious/pull/23/files
        grammar = Grammar(
            """
        filter           = scalar_filter / compound_filter / boolean_filter
        boolean_filter   = open boolean boolean_operands close
        boolean          = "and" / "or"
        boolean_operands = boolean_operand+
        boolean_operand = separator specific_filter
        specific_filter = scalar_filter / compound_filter / boolean_filter
        scalar_filter   = open json_key separator scalar_operator separator json_value close
        compound_filter   = open json_key separator compound_operator separator scalar_filter close
        json_value_or_scalar_filter = json_value / scalar_filter

        json_key        = ~"[A-Z 0-9_.]*"i
        scalar_operator = ":eq" / ":ne" / ":gte" / ":gt" / ":lte" / ":lt" / ":like"
        compound_operator = ":all" / ":any"
        open            = "("
        close           = ")"
        separator       = ","

        json_value = double_string / true_false_null / number

        double_string   = ~'"([^"])*"'
        true_false_null = "true" / "false" / "null"
        number = (int frac exp) / (int exp) / (int frac) / int

        int = "-"? ((digit1to9 digits) / digit)
        frac = "." digits
        exp = e digits
        digits = digit+
        e = "e+" / "e-" / "e" / "E+" / "E-" / "E"
        digit1to9 = ~"[1-9]"
        digit = ~"[0-9]"
        """
        )
        return grammar.parse(filter_string)

    class FilterVisitor(NodeVisitor):
        def visit_filter(self, node, visited_children):
            return visited_children[0]

        def visit_boolean_filter(self, node, visited_children):
            _, boolean, boolean_operands, _ = visited_children
            return {boolean: boolean_operands}

        def visit_boolean(self, node, visited_children):
            return node.text

        def visit_boolean_operand(self, node, visited_children):
            _, specific_filter = visited_children
            return specific_filter

        def visit_specific_filter(self, node, visited_children):
            return visited_children[0]

        def visit_scalar_filter(self, node, visited_children):
            (
                _,
                json_key,
                _,
                operator,
                _,
                json_value,
                _,
            ) = visited_children

            return {
                "name": json_key,
                "op": operator,
                "val": json_value,
            }

        def visit_compound_filter(self, node, visited_children):
            (
                _,
                json_key,
                _,
                operator,
                _,
                scalar_filter,
                _,
            ) = visited_children

            return {
                "name": json_key,
                "op": operator,
                "val": scalar_filter,
            }

        #  XXX no longer used, remove
        def visit_json_value_or_scalar_filter(self, node, visited_children):
            return visited_children[0]

        def visit_json_key(self, node, visited_children):
            return node.text

        def visit_scalar_operator(self, node, visited_children):
            return node.text

        def visit_compound_operator(self, node, visited_children):
            return node.text

        def visit_json_value(self, node, visited_children):
            return json.loads(node.text)

        def generic_visit(self, node, visited_children):
            return visited_children or node

    def get_sqlalchemy_clause(filter_dict):
        """
        Return a SQLAlchemy WHERE clause representing filter_dict.

        Args:
            filter_dict(dict):

            when filter_dict is a scalar:
            {
                "name": "_resource_paths.1",
                "op": ":like",
                "val": "/programs/%"
            }

            when filter_dict is a compound:
            {
                "name": "_resource_paths",
                "op": ":any",
                "val": {
                    "name": "",
                    "op": ":like",
                    "val": "/programs/%"
                }
            }

            when filter_dict is a boolean compound:
            {
                "or": [
                    {
                        "name": "_uploader_id",
                        "op": ":eq",
                        "val": "101"
                    },
                    {
                        "name": "_uploader_id",
                        "op": ":eq",
                        "val": "102"
                    }
                ]
            }

        Returns:
            A SQLAlchemy WHERE clause representing filter_dict.
        """
        if not filter_dict:
            return

        if len(filter_dict) == 1:
            boolean = list(filter_dict.keys())[0]

            boolean_operand_clauses = (
                get_sqlalchemy_clause(c) for c in filter_dict[boolean]
            )
            if boolean == "and":
                return db.and_(boolean_operand_clauses)
            elif boolean == "or":
                return db.or_(boolean_operand_clauses)
            raise

        return add_filter(filter_dict["name"], filter_dict["op"], filter_dict["val"])

    try:
        filter_tree = parse_filter(filter)
        filter_dict = {}
        if filter_tree:
            filter_tree_visitor = FilterVisitor()
            filter_dict = filter_tree_visitor.visit(filter_tree)
    except (IncompleteParseError, ParseError, VisitationError):
        raise HTTPException(
            HTTP_400_BAD_REQUEST, f"filter URL query param syntax is invalid"
        )

    if data:
        query = Metadata.query
        if filter_dict:
            query = query.where(get_sqlalchemy_clause(filter_dict))
        return [
            (metadata.guid, metadata.data)
            for metadata in await query.offset(page * limit).limit(limit).gino.all()
        ]
    else:
        query = db.select([Metadata.guid])
        if filter_dict:
            query = query.where(get_sqlalchemy_clause(filter_dict))
        return [
            row[0]
            for row in await query.offset(page * limit)
            .limit(limit)
            .gino.return_model(False)
            .all()
        ]


@mod.get("/metadata/{guid:path}")
async def get_metadata(guid):
    """Get the metadata of the GUID."""
    metadata = await Metadata.get(guid)
    if metadata:
        return metadata.data
    else:
        raise HTTPException(HTTP_404_NOT_FOUND, f"Not found: {guid}")


def init_app(app):
    app.include_router(mod, tags=["Query"])
