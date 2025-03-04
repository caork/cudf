# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""
DSL nodes for the LogicalPlan of polars.

An IR node is either a source, normal, or a sink. Respectively they
can be considered as functions:

- source: `IO () -> DataFrame`
- normal: `DataFrame -> DataFrame`
- sink: `DataFrame -> IO ()`
"""

from __future__ import annotations

import itertools
import json
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pyarrow as pa
from typing_extensions import assert_never

import polars as pl

import pylibcudf as plc

import cudf_polars.dsl.expr as expr
from cudf_polars.containers import Column, DataFrame
from cudf_polars.dsl.nodebase import Node
from cudf_polars.dsl.to_ast import to_parquet_filter
from cudf_polars.utils import dtypes

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, MutableMapping, Sequence
    from typing import Literal

    from cudf_polars.typing import Schema


__all__ = [
    "IR",
    "PythonScan",
    "Scan",
    "Cache",
    "DataFrameScan",
    "Select",
    "GroupBy",
    "Join",
    "HStack",
    "Distinct",
    "Sort",
    "Slice",
    "Filter",
    "Projection",
    "MapFunction",
    "Union",
    "HConcat",
]


def broadcast(*columns: Column, target_length: int | None = None) -> list[Column]:
    """
    Broadcast a sequence of columns to a common length.

    Parameters
    ----------
    columns
        Columns to broadcast.
    target_length
        Optional length to broadcast to. If not provided, uses the
        non-unit length of existing columns.

    Returns
    -------
    List of broadcasted columns all of the same length.

    Raises
    ------
    RuntimeError
        If broadcasting is not possible.

    Notes
    -----
    In evaluation of a set of expressions, polars type-puns length-1
    columns with scalars. When we insert these into a DataFrame
    object, we need to ensure they are of equal length. This function
    takes some columns, some of which may be length-1 and ensures that
    all length-1 columns are broadcast to the length of the others.

    Broadcasting is only possible if the set of lengths of the input
    columns is a subset of ``{1, n}`` for some (fixed) ``n``. If
    ``target_length`` is provided and not all columns are length-1
    (i.e. ``n != 1``), then ``target_length`` must be equal to ``n``.
    """
    if len(columns) == 0:
        return []
    lengths: set[int] = {column.obj.size() for column in columns}
    if lengths == {1}:
        if target_length is None:
            return list(columns)
        nrows = target_length
    else:
        try:
            (nrows,) = lengths.difference([1])
        except ValueError as e:
            raise RuntimeError("Mismatching column lengths") from e
        if target_length is not None and nrows != target_length:
            raise RuntimeError(
                f"Cannot broadcast columns of length {nrows=} to {target_length=}"
            )
    return [
        column
        if column.obj.size() != 1
        else Column(
            plc.Column.from_scalar(column.obj_scalar, nrows),
            is_sorted=plc.types.Sorted.YES,
            order=plc.types.Order.ASCENDING,
            null_order=plc.types.NullOrder.BEFORE,
            name=column.name,
        )
        for column in columns
    ]


class IR(Node["IR"]):
    """Abstract plan node, representing an unevaluated dataframe."""

    __slots__ = ("schema",)
    # This annotation is needed because of https://github.com/python/mypy/issues/17981
    _non_child: ClassVar[tuple[str, ...]] = ("schema",)
    schema: Schema
    """Mapping from column names to their data types."""

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of node, treating schema dictionary.

        Since the schema is a dictionary, even though it is morally
        immutable, it is not hashable. We therefore convert it to
        tuples for hashing purposes.
        """
        # Schema is the first constructor argument
        args = self._ctor_arguments(self.children)[1:]
        schema_hash = tuple(self.schema.items())
        return (type(self), schema_hash, args)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """
        Evaluate the node and return a dataframe.

        Parameters
        ----------
        cache
            Mapping from cached node ids to constructed DataFrames.
            Used to implement evaluation of the `Cache` node.

        Returns
        -------
        DataFrame (on device) representing the evaluation of this plan
        node.

        Raises
        ------
        NotImplementedError
            If we couldn't evaluate things. Ideally this should not occur,
            since the translation phase should pick up things that we
            cannot handle.
        """
        raise NotImplementedError(
            f"Evaluation of plan {type(self).__name__}"
        )  # pragma: no cover


class PythonScan(IR):
    """Representation of input from a python function."""

    __slots__ = ("options", "predicate")
    _non_child = ("schema", "options", "predicate")
    options: Any
    """Arbitrary options."""
    predicate: expr.NamedExpr | None
    """Filter to apply to the constructed dataframe before returning it."""

    def __init__(self, schema: Schema, options: Any, predicate: expr.NamedExpr | None):
        self.schema = schema
        self.options = options
        self.predicate = predicate
        self.children = ()
        raise NotImplementedError("PythonScan not implemented")


class Scan(IR):
    """Input from files."""

    __slots__ = (
        "typ",
        "reader_options",
        "cloud_options",
        "paths",
        "with_columns",
        "skip_rows",
        "n_rows",
        "row_index",
        "predicate",
    )
    _non_child = (
        "schema",
        "typ",
        "reader_options",
        "cloud_options",
        "paths",
        "with_columns",
        "skip_rows",
        "n_rows",
        "row_index",
        "predicate",
    )
    typ: str
    """What type of file are we reading? Parquet, CSV, etc..."""
    reader_options: dict[str, Any]
    """Reader-specific options, as dictionary."""
    cloud_options: dict[str, Any] | None
    """Cloud-related authentication options, currently ignored."""
    paths: list[str]
    """List of paths to read from."""
    with_columns: list[str] | None
    """Projected columns to return."""
    skip_rows: int
    """Rows to skip at the start when reading."""
    n_rows: int
    """Number of rows to read after skipping."""
    row_index: tuple[str, int] | None
    """If not None add an integer index column of the given name."""
    predicate: expr.NamedExpr | None
    """Mask to apply to the read dataframe."""

    def __init__(
        self,
        schema: Schema,
        typ: str,
        reader_options: dict[str, Any],
        cloud_options: dict[str, Any] | None,
        paths: list[str],
        with_columns: list[str] | None,
        skip_rows: int,
        n_rows: int,
        row_index: tuple[str, int] | None,
        predicate: expr.NamedExpr | None,
    ):
        self.schema = schema
        self.typ = typ
        self.reader_options = reader_options
        self.cloud_options = cloud_options
        self.paths = paths
        self.with_columns = with_columns
        self.skip_rows = skip_rows
        self.n_rows = n_rows
        self.row_index = row_index
        self.predicate = predicate
        self.children = ()
        if self.typ not in ("csv", "parquet", "ndjson"):  # pragma: no cover
            # This line is unhittable ATM since IPC/Anonymous scan raise
            # on the polars side
            raise NotImplementedError(f"Unhandled scan type: {self.typ}")
        if self.typ == "ndjson" and (self.n_rows != -1 or self.skip_rows != 0):
            raise NotImplementedError("row limit in scan for json reader")
        if self.skip_rows < 0:
            # TODO: polars has this implemented for parquet,
            # maybe we can do this too?
            raise NotImplementedError("slice pushdown for negative slices")
        if self.typ == "csv" and self.skip_rows != 0:  # pragma: no cover
            # This comes from slice pushdown, but that
            # optimization doesn't happen right now
            raise NotImplementedError("skipping rows in CSV reader")
        if self.cloud_options is not None and any(
            self.cloud_options.get(k) is not None for k in ("aws", "azure", "gcp")
        ):
            raise NotImplementedError(
                "Read from cloud storage"
            )  # pragma: no cover; no test yet
        if any(p.startswith("https://") for p in self.paths):
            raise NotImplementedError("Read from https")
        if self.typ == "csv":
            if self.reader_options["skip_rows_after_header"] != 0:
                raise NotImplementedError("Skipping rows after header in CSV reader")
            parse_options = self.reader_options["parse_options"]
            if (
                null_values := parse_options["null_values"]
            ) is not None and "Named" in null_values:
                raise NotImplementedError(
                    "Per column null value specification not supported for CSV reader"
                )
            if (
                comment := parse_options["comment_prefix"]
            ) is not None and "Multi" in comment:
                raise NotImplementedError(
                    "Multi-character comment prefix not supported for CSV reader"
                )
            if not self.reader_options["has_header"]:
                # Need to do some file introspection to get the number
                # of columns so that column projection works right.
                raise NotImplementedError("Reading CSV without header")
        elif self.typ == "ndjson":
            # TODO: consider handling the low memory option here
            # (maybe use chunked JSON reader)
            if self.reader_options["ignore_errors"]:
                raise NotImplementedError(
                    "ignore_errors is not supported in the JSON reader"
                )
        elif (
            self.typ == "parquet"
            and self.row_index is not None
            and self.with_columns is not None
            and len(self.with_columns) == 0
        ):
            raise NotImplementedError(
                "Reading only parquet metadata to produce row index."
            )

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of the node.

        The options dictionaries are serialised for hashing purposes
        as json strings.
        """
        schema_hash = tuple(self.schema.items())
        return (
            type(self),
            schema_hash,
            self.typ,
            json.dumps(self.reader_options),
            json.dumps(self.cloud_options),
            tuple(self.paths),
            tuple(self.with_columns) if self.with_columns is not None else None,
            self.skip_rows,
            self.n_rows,
            self.row_index,
            self.predicate,
        )

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        with_columns = self.with_columns
        row_index = self.row_index
        n_rows = self.n_rows
        if self.typ == "csv":
            parse_options = self.reader_options["parse_options"]
            sep = chr(parse_options["separator"])
            quote = chr(parse_options["quote_char"])
            eol = chr(parse_options["eol_char"])
            if self.reader_options["schema"] is not None:
                # Reader schema provides names
                column_names = list(self.reader_options["schema"]["fields"].keys())
            else:
                # file provides column names
                column_names = None
            usecols = with_columns
            # TODO: support has_header=False
            header = 0

            # polars defaults to no null recognition
            null_values = [""]
            if parse_options["null_values"] is not None:
                ((typ, nulls),) = parse_options["null_values"].items()
                if typ == "AllColumnsSingle":
                    # Single value
                    null_values.append(nulls)
                else:
                    # List of values
                    null_values.extend(nulls)
            if parse_options["comment_prefix"] is not None:
                comment = chr(parse_options["comment_prefix"]["Single"])
            else:
                comment = None
            decimal = "," if parse_options["decimal_comma"] else "."

            # polars skips blank lines at the beginning of the file
            pieces = []
            read_partial = n_rows != -1
            for p in self.paths:
                skiprows = self.reader_options["skip_rows"]
                path = Path(p)
                with path.open() as f:
                    while f.readline() == "\n":
                        skiprows += 1
                tbl_w_meta = plc.io.csv.read_csv(
                    plc.io.SourceInfo([path]),
                    delimiter=sep,
                    quotechar=quote,
                    lineterminator=eol,
                    col_names=column_names,
                    header=header,
                    usecols=usecols,
                    na_filter=True,
                    na_values=null_values,
                    keep_default_na=False,
                    skiprows=skiprows,
                    comment=comment,
                    decimal=decimal,
                    dtypes=self.schema,
                    nrows=n_rows,
                )
                pieces.append(tbl_w_meta)
                if read_partial:
                    n_rows -= tbl_w_meta.tbl.num_rows()
                    if n_rows <= 0:
                        break
            tables, colnames = zip(
                *(
                    (piece.tbl, piece.column_names(include_children=False))
                    for piece in pieces
                ),
                strict=True,
            )
            df = DataFrame.from_table(
                plc.concatenate.concatenate(list(tables)),
                colnames[0],
            )
        elif self.typ == "parquet":
            filters = None
            if self.predicate is not None and self.row_index is None:
                # Can't apply filters during read if we have a row index.
                filters = to_parquet_filter(self.predicate.value)
            tbl_w_meta = plc.io.parquet.read_parquet(
                plc.io.SourceInfo(self.paths),
                columns=with_columns,
                filters=filters,
                nrows=n_rows,
                skip_rows=self.skip_rows,
            )
            df = DataFrame.from_table(
                tbl_w_meta.tbl,
                # TODO: consider nested column names?
                tbl_w_meta.column_names(include_children=False),
            )
            if filters is not None:
                # Mask must have been applied.
                return df
        elif self.typ == "ndjson":
            json_schema: list[tuple[str, str, list]] = [
                (name, typ, []) for name, typ in self.schema.items()
            ]
            plc_tbl_w_meta = plc.io.json.read_json(
                plc.io.SourceInfo(self.paths),
                lines=True,
                dtypes=json_schema,
                prune_columns=True,
            )
            # TODO: I don't think cudf-polars supports nested types in general right now
            # (but when it does, we should pass child column names from nested columns in)
            df = DataFrame.from_table(
                plc_tbl_w_meta.tbl, plc_tbl_w_meta.column_names(include_children=False)
            )
            col_order = list(self.schema.keys())
            # TODO: remove condition when dropping support for polars 1.0
            # https://github.com/pola-rs/polars/pull/17363
            if row_index is not None and row_index[0] in self.schema:
                col_order.remove(row_index[0])
            if col_order is not None:
                df = df.select(col_order)
        else:
            raise NotImplementedError(
                f"Unhandled scan type: {self.typ}"
            )  # pragma: no cover; post init trips first
        if row_index is not None:
            name, offset = row_index
            dtype = self.schema[name]
            step = plc.interop.from_arrow(
                pa.scalar(1, type=plc.interop.to_arrow(dtype))
            )
            init = plc.interop.from_arrow(
                pa.scalar(offset, type=plc.interop.to_arrow(dtype))
            )
            index = Column(
                plc.filling.sequence(df.num_rows, init, step),
                is_sorted=plc.types.Sorted.YES,
                order=plc.types.Order.ASCENDING,
                null_order=plc.types.NullOrder.AFTER,
                name=name,
            )
            df = DataFrame([index, *df.columns])
        assert all(
            c.obj.type() == self.schema[name] for name, c in df.column_map.items()
        )
        if self.predicate is None:
            return df
        else:
            (mask,) = broadcast(self.predicate.evaluate(df), target_length=df.num_rows)
            return df.filter(mask)


class Cache(IR):
    """
    Return a cached plan node.

    Used for CSE at the plan level.
    """

    __slots__ = ("key",)
    _non_child = ("schema", "key")
    key: int
    """The cache key."""

    def __init__(self, schema: Schema, key: int, value: IR):
        self.schema = schema
        self.key = key
        self.children = (value,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        try:
            return cache[self.key]
        except KeyError:
            (value,) = self.children
            return cache.setdefault(self.key, value.evaluate(cache=cache))


class DataFrameScan(IR):
    """
    Input from an existing polars DataFrame.

    This typically arises from ``q.collect().lazy()``
    """

    __slots__ = ("df", "projection", "predicate")
    _non_child = ("schema", "df", "projection", "predicate")
    df: Any
    """Polars LazyFrame object."""
    projection: tuple[str, ...] | None
    """List of columns to project out."""
    predicate: expr.NamedExpr | None
    """Mask to apply."""

    def __init__(
        self,
        schema: Schema,
        df: Any,
        projection: Sequence[str] | None,
        predicate: expr.NamedExpr | None,
    ):
        self.schema = schema
        self.df = df
        self.projection = tuple(projection) if projection is not None else None
        self.predicate = predicate
        self.children = ()

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of the node.

        The (heavy) dataframe object is hashed as its id, so this is
        not stable across runs, or repeat instances of the same equal dataframes.
        """
        schema_hash = tuple(self.schema.items())
        return (type(self), schema_hash, id(self.df), self.projection, self.predicate)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        pdf = pl.DataFrame._from_pydf(self.df)
        if self.projection is not None:
            pdf = pdf.select(self.projection)
        df = DataFrame.from_polars(pdf)
        assert all(
            c.obj.type() == dtype
            for c, dtype in zip(df.columns, self.schema.values(), strict=True)
        )
        if self.predicate is not None:
            (mask,) = broadcast(self.predicate.evaluate(df), target_length=df.num_rows)
            return df.filter(mask)
        else:
            return df


class Select(IR):
    """Produce a new dataframe selecting given expressions from an input."""

    __slots__ = ("exprs", "should_broadcast")
    _non_child = ("schema", "exprs", "should_broadcast")
    exprs: tuple[expr.NamedExpr, ...]
    """List of expressions to evaluate to form the new dataframe."""
    should_broadcast: bool
    """Should columns be broadcast?"""

    def __init__(
        self,
        schema: Schema,
        exprs: Sequence[expr.NamedExpr],
        should_broadcast: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.exprs = tuple(exprs)
        self.should_broadcast = should_broadcast
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        # Handle any broadcasting
        columns = [e.evaluate(df) for e in self.exprs]
        if self.should_broadcast:
            columns = broadcast(*columns)
        return DataFrame(columns)


class Reduce(IR):
    """
    Produce a new dataframe selecting given expressions from an input.

    This is a special case of :class:`Select` where all outputs are a single row.
    """

    __slots__ = ("exprs",)
    _non_child = ("schema", "exprs")
    exprs: tuple[expr.NamedExpr, ...]
    """List of expressions to evaluate to form the new dataframe."""

    def __init__(
        self, schema: Schema, exprs: Sequence[expr.NamedExpr], df: IR
    ):  # pragma: no cover; polars doesn't emit this node yet
        self.schema = schema
        self.exprs = tuple(exprs)
        self.children = (df,)

    def evaluate(
        self, *, cache: MutableMapping[int, DataFrame]
    ) -> DataFrame:  # pragma: no cover; polars doesn't emit this node yet
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        columns = broadcast(*(e.evaluate(df) for e in self.exprs))
        assert all(column.obj.size() == 1 for column in columns)
        return DataFrame(columns)


class GroupBy(IR):
    """Perform a groupby."""

    __slots__ = (
        "agg_requests",
        "keys",
        "maintain_order",
        "options",
        "agg_infos",
    )
    _non_child = ("schema", "keys", "agg_requests", "maintain_order", "options")
    keys: tuple[expr.NamedExpr, ...]
    """Grouping keys."""
    agg_requests: tuple[expr.NamedExpr, ...]
    """Aggregation expressions."""
    maintain_order: bool
    """Preserve order in groupby."""
    options: Any
    """Arbitrary options."""

    def __init__(
        self,
        schema: Schema,
        keys: Sequence[expr.NamedExpr],
        agg_requests: Sequence[expr.NamedExpr],
        maintain_order: bool,  # noqa: FBT001
        options: Any,
        df: IR,
    ):
        self.schema = schema
        self.keys = tuple(keys)
        self.agg_requests = tuple(agg_requests)
        self.maintain_order = maintain_order
        self.options = options
        self.children = (df,)
        if self.options.rolling:
            raise NotImplementedError(
                "rolling window/groupby"
            )  # pragma: no cover; rollingwindow constructor has already raised
        if self.options.dynamic:
            raise NotImplementedError("dynamic group by")
        if any(GroupBy.check_agg(a.value) > 1 for a in self.agg_requests):
            raise NotImplementedError("Nested aggregations in groupby")
        self.agg_infos = [req.collect_agg(depth=0) for req in self.agg_requests]

    @staticmethod
    def check_agg(agg: expr.Expr) -> int:
        """
        Determine if we can handle an aggregation expression.

        Parameters
        ----------
        agg
            Expression to check

        Returns
        -------
        depth of nesting

        Raises
        ------
        NotImplementedError
            For unsupported expression nodes.
        """
        if isinstance(agg, (expr.BinOp, expr.Cast, expr.UnaryFunction)):
            return max(GroupBy.check_agg(child) for child in agg.children)
        elif isinstance(agg, expr.Agg):
            return 1 + max(GroupBy.check_agg(child) for child in agg.children)
        elif isinstance(agg, (expr.Len, expr.Col, expr.Literal, expr.LiteralColumn)):
            return 0
        else:
            raise NotImplementedError(f"No handler for {agg=}")

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        keys = broadcast(
            *(k.evaluate(df) for k in self.keys), target_length=df.num_rows
        )
        sorted = (
            plc.types.Sorted.YES
            if all(k.is_sorted for k in keys)
            else plc.types.Sorted.NO
        )
        grouper = plc.groupby.GroupBy(
            plc.Table([k.obj for k in keys]),
            null_handling=plc.types.NullPolicy.INCLUDE,
            keys_are_sorted=sorted,
            column_order=[k.order for k in keys],
            null_precedence=[k.null_order for k in keys],
        )
        # TODO: uniquify
        requests = []
        replacements: list[expr.Expr] = []
        for info in self.agg_infos:
            for pre_eval, req, rep in info.requests:
                if pre_eval is None:
                    # A count aggregation, doesn't touch the column,
                    # but we need to have one. Rather than evaluating
                    # one, just use one of the key columns.
                    col = keys[0].obj
                else:
                    col = pre_eval.evaluate(df).obj
                requests.append(plc.groupby.GroupByRequest(col, [req]))
                replacements.append(rep)
        group_keys, raw_tables = grouper.aggregate(requests)
        raw_columns: list[Column] = []
        for i, table in enumerate(raw_tables):
            (column,) = table.columns()
            raw_columns.append(Column(column, name=f"tmp{i}"))
        mapping = dict(zip(replacements, raw_columns, strict=True))
        result_keys = [
            Column(grouped_key, name=key.name)
            for key, grouped_key in zip(keys, group_keys.columns(), strict=True)
        ]
        result_subs = DataFrame(raw_columns)
        results = [
            req.evaluate(result_subs, mapping=mapping) for req in self.agg_requests
        ]
        broadcasted = broadcast(*result_keys, *results)
        # Handle order preservation of groups
        if self.maintain_order and not sorted:
            # The order we want
            want = plc.stream_compaction.stable_distinct(
                plc.Table([k.obj for k in keys]),
                list(range(group_keys.num_columns())),
                plc.stream_compaction.DuplicateKeepOption.KEEP_FIRST,
                plc.types.NullEquality.EQUAL,
                plc.types.NanEquality.ALL_EQUAL,
            )
            # The order we have
            have = plc.Table([key.obj for key in broadcasted[: len(keys)]])

            # We know an inner join is OK because by construction
            # want and have are permutations of each other.
            left_order, right_order = plc.join.inner_join(
                want, have, plc.types.NullEquality.EQUAL
            )
            # Now left_order is an arbitrary permutation of the ordering we
            # want, and right_order is a matching permutation of the ordering
            # we have. To get to the original ordering, we need
            # left_order == iota(nrows), with right_order permuted
            # appropriately. This can be obtained by sorting
            # right_order by left_order.
            (right_order,) = plc.sorting.sort_by_key(
                plc.Table([right_order]),
                plc.Table([left_order]),
                [plc.types.Order.ASCENDING],
                [plc.types.NullOrder.AFTER],
            ).columns()
            ordered_table = plc.copying.gather(
                plc.Table([col.obj for col in broadcasted]),
                right_order,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
            )
            broadcasted = [
                Column(reordered, name=old.name)
                for reordered, old in zip(
                    ordered_table.columns(), broadcasted, strict=True
                )
            ]
        return DataFrame(broadcasted).slice(self.options.slice)


class Join(IR):
    """A join of two dataframes."""

    __slots__ = ("left_on", "right_on", "options")
    _non_child = ("schema", "left_on", "right_on", "options")
    left_on: tuple[expr.NamedExpr, ...]
    """List of expressions used as keys in the left frame."""
    right_on: tuple[expr.NamedExpr, ...]
    """List of expressions used as keys in the right frame."""
    options: tuple[
        Literal["inner", "left", "right", "full", "semi", "anti", "cross"],
        bool,
        tuple[int, int] | None,
        str,
        bool,
    ]
    """
    tuple of options:
    - how: join type
    - join_nulls: do nulls compare equal?
    - slice: optional slice to perform after joining.
    - suffix: string suffix for right columns if names match
    - coalesce: should key columns be coalesced (only makes sense for outer joins)
    """

    def __init__(
        self,
        schema: Schema,
        left_on: Sequence[expr.NamedExpr],
        right_on: Sequence[expr.NamedExpr],
        options: Any,
        left: IR,
        right: IR,
    ):
        self.schema = schema
        self.left_on = tuple(left_on)
        self.right_on = tuple(right_on)
        self.options = options
        self.children = (left, right)
        if any(
            isinstance(e.value, expr.Literal)
            for e in itertools.chain(self.left_on, self.right_on)
        ):
            raise NotImplementedError("Join with literal as join key.")

    @staticmethod
    @cache
    def _joiners(
        how: Literal["inner", "left", "right", "full", "semi", "anti"],
    ) -> tuple[
        Callable, plc.copying.OutOfBoundsPolicy, plc.copying.OutOfBoundsPolicy | None
    ]:
        if how == "inner":
            return (
                plc.join.inner_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
            )
        elif how == "left" or how == "right":
            return (
                plc.join.left_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
            )
        elif how == "full":
            return (
                plc.join.full_join,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
            )
        elif how == "semi":
            return (
                plc.join.left_semi_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                None,
            )
        elif how == "anti":
            return (
                plc.join.left_anti_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                None,
            )
        assert_never(how)

    def _reorder_maps(
        self,
        left_rows: int,
        lg: plc.Column,
        left_policy: plc.copying.OutOfBoundsPolicy,
        right_rows: int,
        rg: plc.Column,
        right_policy: plc.copying.OutOfBoundsPolicy,
    ) -> list[plc.Column]:
        """
        Reorder gather maps to satisfy polars join order restrictions.

        Parameters
        ----------
        left_rows
            Number of rows in left table
        lg
            Left gather map
        left_policy
            Nullify policy for left map
        right_rows
            Number of rows in right table
        rg
            Right gather map
        right_policy
            Nullify policy for right map

        Returns
        -------
        list of reordered left and right gather maps.

        Notes
        -----
        For a left join, the polars result preserves the order of the
        left keys, and is stable wrt the right keys. For all other
        joins, there is no order obligation.
        """
        dt = plc.interop.to_arrow(plc.types.SIZE_TYPE)
        init = plc.interop.from_arrow(pa.scalar(0, type=dt))
        step = plc.interop.from_arrow(pa.scalar(1, type=dt))
        left_order = plc.copying.gather(
            plc.Table([plc.filling.sequence(left_rows, init, step)]), lg, left_policy
        )
        right_order = plc.copying.gather(
            plc.Table([plc.filling.sequence(right_rows, init, step)]), rg, right_policy
        )
        return plc.sorting.stable_sort_by_key(
            plc.Table([lg, rg]),
            plc.Table([*left_order.columns(), *right_order.columns()]),
            [plc.types.Order.ASCENDING, plc.types.Order.ASCENDING],
            [plc.types.NullOrder.AFTER, plc.types.NullOrder.AFTER],
        ).columns()

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        left, right = (c.evaluate(cache=cache) for c in self.children)
        how, join_nulls, zlice, suffix, coalesce = self.options
        if how == "cross":
            # Separate implementation, since cross_join returns the
            # result, not the gather maps
            columns = plc.join.cross_join(left.table, right.table).columns()
            left_cols = [
                Column(new, name=old.name).sorted_like(old)
                for new, old in zip(
                    columns[: left.num_columns], left.columns, strict=True
                )
            ]
            right_cols = [
                Column(
                    new,
                    name=name
                    if name not in left.column_names_set
                    else f"{name}{suffix}",
                )
                for new, name in zip(
                    columns[left.num_columns :], right.column_names, strict=True
                )
            ]
            return DataFrame([*left_cols, *right_cols]).slice(zlice)
        # TODO: Waiting on clarity based on https://github.com/pola-rs/polars/issues/17184
        left_on = DataFrame(broadcast(*(e.evaluate(left) for e in self.left_on)))
        right_on = DataFrame(broadcast(*(e.evaluate(right) for e in self.right_on)))
        null_equality = (
            plc.types.NullEquality.EQUAL
            if join_nulls
            else plc.types.NullEquality.UNEQUAL
        )
        join_fn, left_policy, right_policy = Join._joiners(how)
        if right_policy is None:
            # Semi join
            lg = join_fn(left_on.table, right_on.table, null_equality)
            table = plc.copying.gather(left.table, lg, left_policy)
            result = DataFrame.from_table(table, left.column_names)
        else:
            if how == "right":
                # Right join is a left join with the tables swapped
                left, right = right, left
                left_on, right_on = right_on, left_on
            lg, rg = join_fn(left_on.table, right_on.table, null_equality)
            if how == "left" or how == "right":
                # Order of left table is preserved
                lg, rg = self._reorder_maps(
                    left.num_rows, lg, left_policy, right.num_rows, rg, right_policy
                )
            if coalesce and how == "inner":
                right = right.discard_columns(right_on.column_names_set)
            left = DataFrame.from_table(
                plc.copying.gather(left.table, lg, left_policy), left.column_names
            )
            right = DataFrame.from_table(
                plc.copying.gather(right.table, rg, right_policy), right.column_names
            )
            if coalesce and how != "inner":
                left = left.with_columns(
                    (
                        Column(
                            plc.replace.replace_nulls(left_col.obj, right_col.obj),
                            name=left_col.name,
                        )
                        for left_col, right_col in zip(
                            left.select_columns(left_on.column_names_set),
                            right.select_columns(right_on.column_names_set),
                            strict=True,
                        )
                    ),
                    replace_only=True,
                )
                right = right.discard_columns(right_on.column_names_set)
            if how == "right":
                # Undo the swap for right join before gluing together.
                left, right = right, left
            right = right.rename_columns(
                {
                    name: f"{name}{suffix}"
                    for name in right.column_names
                    if name in left.column_names_set
                }
            )
            result = left.with_columns(right.columns)
        return result.slice(zlice)


class HStack(IR):
    """Add new columns to a dataframe."""

    __slots__ = ("columns", "should_broadcast")
    _non_child = ("schema", "columns", "should_broadcast")
    should_broadcast: bool
    """Should the resulting evaluated columns be broadcast to the same length."""

    def __init__(
        self,
        schema: Schema,
        columns: Sequence[expr.NamedExpr],
        should_broadcast: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.columns = tuple(columns)
        self.should_broadcast = should_broadcast
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        columns = [c.evaluate(df) for c in self.columns]
        if self.should_broadcast:
            columns = broadcast(*columns, target_length=df.num_rows)
        else:
            # Polars ensures this is true, but let's make sure nothing
            # went wrong. In this case, the parent node is a
            # guaranteed to be a Select which will take care of making
            # sure that everything is the same length. The result
            # table that might have mismatching column lengths will
            # never be turned into a pylibcudf Table with all columns
            # by the Select, which is why this is safe.
            assert all(e.name.startswith("__POLARS_CSER_0x") for e in self.columns)
        return df.with_columns(columns)


class Distinct(IR):
    """Produce a new dataframe with distinct rows."""

    __slots__ = ("keep", "subset", "zlice", "stable")
    _non_child = ("schema", "keep", "subset", "zlice", "stable")
    keep: plc.stream_compaction.DuplicateKeepOption
    """Which distinct value to keep."""
    subset: frozenset[str] | None
    """Which columns should be used to define distinctness. If None,
    then all columns are used."""
    zlice: tuple[int, int] | None
    """Optional slice to apply to the result."""
    stable: bool
    """Should the result maintain ordering."""

    def __init__(
        self,
        schema: Schema,
        keep: plc.stream_compaction.DuplicateKeepOption,
        subset: frozenset[str] | None,
        zlice: tuple[int, int] | None,
        stable: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.keep = keep
        self.subset = subset
        self.zlice = zlice
        self.stable = stable
        self.children = (df,)

    _KEEP_MAP: ClassVar[dict[str, plc.stream_compaction.DuplicateKeepOption]] = {
        "first": plc.stream_compaction.DuplicateKeepOption.KEEP_FIRST,
        "last": plc.stream_compaction.DuplicateKeepOption.KEEP_LAST,
        "none": plc.stream_compaction.DuplicateKeepOption.KEEP_NONE,
        "any": plc.stream_compaction.DuplicateKeepOption.KEEP_ANY,
    }

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        if self.subset is None:
            indices = list(range(df.num_columns))
            keys_sorted = all(c.is_sorted for c in df.column_map.values())
        else:
            indices = [i for i, k in enumerate(df.column_names) if k in self.subset]
            keys_sorted = all(df.column_map[name].is_sorted for name in self.subset)
        if keys_sorted:
            table = plc.stream_compaction.unique(
                df.table,
                indices,
                self.keep,
                plc.types.NullEquality.EQUAL,
            )
        else:
            distinct = (
                plc.stream_compaction.stable_distinct
                if self.stable
                else plc.stream_compaction.distinct
            )
            table = distinct(
                df.table,
                indices,
                self.keep,
                plc.types.NullEquality.EQUAL,
                plc.types.NanEquality.ALL_EQUAL,
            )
        # TODO: Is this sortedness setting correct
        result = DataFrame(
            [
                Column(new, name=old.name).sorted_like(old)
                for new, old in zip(table.columns(), df.columns, strict=True)
            ]
        )
        if keys_sorted or self.stable:
            result = result.sorted_like(df)
        return result.slice(self.zlice)


class Sort(IR):
    """Sort a dataframe."""

    __slots__ = ("by", "order", "null_order", "stable", "zlice")
    _non_child = ("schema", "by", "order", "null_order", "stable", "zlice")
    by: tuple[expr.NamedExpr, ...]
    """Sort keys."""
    order: tuple[plc.types.Order, ...]
    """Sort order for each sort key."""
    null_order: tuple[plc.types.NullOrder, ...]
    """Null sorting location for each sort key."""
    stable: bool
    """Should the sort be stable?"""
    zlice: tuple[int, int] | None
    """Optional slice to apply to the result."""

    def __init__(
        self,
        schema: Schema,
        by: Sequence[expr.NamedExpr],
        order: Sequence[plc.types.Order],
        null_order: Sequence[plc.types.NullOrder],
        stable: bool,  # noqa: FBT001
        zlice: tuple[int, int] | None,
        df: IR,
    ):
        self.schema = schema
        self.by = tuple(by)
        self.order = tuple(order)
        self.null_order = tuple(null_order)
        self.stable = stable
        self.zlice = zlice
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        sort_keys = broadcast(
            *(k.evaluate(df) for k in self.by), target_length=df.num_rows
        )
        # TODO: More robust identification here.
        keys_in_result = {
            k.name: i
            for i, k in enumerate(sort_keys)
            if k.name in df.column_map and k.obj is df.column_map[k.name].obj
        }
        do_sort = (
            plc.sorting.stable_sort_by_key if self.stable else plc.sorting.sort_by_key
        )
        table = do_sort(
            df.table,
            plc.Table([k.obj for k in sort_keys]),
            list(self.order),
            list(self.null_order),
        )
        columns: list[Column] = []
        for name, c in zip(df.column_map, table.columns(), strict=True):
            column = Column(c, name=name)
            # If a sort key is in the result table, set the sortedness property
            if name in keys_in_result:
                i = keys_in_result[name]
                column = column.set_sorted(
                    is_sorted=plc.types.Sorted.YES,
                    order=self.order[i],
                    null_order=self.null_order[i],
                )
            columns.append(column)
        return DataFrame(columns).slice(self.zlice)


class Slice(IR):
    """Slice a dataframe."""

    __slots__ = ("offset", "length")
    _non_child = ("schema", "offset", "length")
    offset: int
    """Start of the slice."""
    length: int
    """Length of the slice."""

    def __init__(self, schema: Schema, offset: int, length: int, df: IR):
        self.schema = schema
        self.offset = offset
        self.length = length
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        return df.slice((self.offset, self.length))


class Filter(IR):
    """Filter a dataframe with a boolean mask."""

    __slots__ = ("mask",)
    _non_child = ("schema", "mask")
    mask: expr.NamedExpr
    """Expression to produce the filter mask."""

    def __init__(self, schema: Schema, mask: expr.NamedExpr, df: IR):
        self.schema = schema
        self.mask = mask
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        (mask,) = broadcast(self.mask.evaluate(df), target_length=df.num_rows)
        return df.filter(mask)


class Projection(IR):
    """Select a subset of columns from a dataframe."""

    __slots__ = ()
    _non_child = ("schema",)

    def __init__(self, schema: Schema, df: IR):
        self.schema = schema
        self.children = (df,)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        df = child.evaluate(cache=cache)
        # This can reorder things.
        columns = broadcast(
            *(df.column_map[name] for name in self.schema), target_length=df.num_rows
        )
        return DataFrame(columns)


class MapFunction(IR):
    """Apply some function to a dataframe."""

    __slots__ = ("name", "options")
    _non_child = ("schema", "name", "options")
    name: str
    """Name of the function to apply"""
    options: Any
    """Arbitrary name-specific options"""

    _NAMES: ClassVar[frozenset[str]] = frozenset(
        [
            "rechunk",
            # libcudf merge is not stable wrt order of inputs, since
            # it uses a priority queue to manage the tables it produces.
            # See: https://github.com/rapidsai/cudf/issues/16010
            # "merge_sorted",
            "rename",
            "explode",
            "unpivot",
        ]
    )

    def __init__(self, schema: Schema, name: str, options: Any, df: IR):
        self.schema = schema
        self.name = name
        self.options = options
        self.children = (df,)
        if self.name not in MapFunction._NAMES:
            raise NotImplementedError(f"Unhandled map function {self.name}")
        if self.name == "explode":
            (to_explode,) = self.options
            if len(to_explode) > 1:
                # TODO: straightforward, but need to error check
                # polars requires that all to-explode columns have the
                # same sub-shapes
                raise NotImplementedError("Explode with more than one column")
        elif self.name == "rename":
            old, new, _ = self.options
            # TODO: perhaps polars should validate renaming in the IR?
            if len(new) != len(set(new)) or (
                set(new) & (set(df.schema.keys()) - set(old))
            ):
                raise NotImplementedError("Duplicate new names in rename.")
        elif self.name == "unpivot":
            indices, pivotees, variable_name, value_name = self.options
            value_name = "value" if value_name is None else value_name
            variable_name = "variable" if variable_name is None else variable_name
            if len(pivotees) == 0:
                index = frozenset(indices)
                pivotees = [name for name in df.schema if name not in index]
            if not all(
                dtypes.can_cast(df.schema[p], self.schema[value_name]) for p in pivotees
            ):
                raise NotImplementedError(
                    "Unpivot cannot cast all input columns to "
                    f"{self.schema[value_name].id()}"
                )
            self.options = (tuple(indices), tuple(pivotees), variable_name, value_name)

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        (child,) = self.children
        if self.name == "rechunk":
            # No-op in our data model
            # Don't think this appears in a plan tree from python
            return child.evaluate(cache=cache)  # pragma: no cover
        elif self.name == "rename":
            df = child.evaluate(cache=cache)
            # final tag is "swapping" which is useful for the
            # optimiser (it blocks some pushdown operations)
            old, new, _ = self.options
            return df.rename_columns(dict(zip(old, new, strict=True)))
        elif self.name == "explode":
            df = child.evaluate(cache=cache)
            ((to_explode,),) = self.options
            index = df.column_names.index(to_explode)
            subset = df.column_names_set - {to_explode}
            return DataFrame.from_table(
                plc.lists.explode_outer(df.table, index), df.column_names
            ).sorted_like(df, subset=subset)
        elif self.name == "unpivot":
            indices, pivotees, variable_name, value_name = self.options
            npiv = len(pivotees)
            df = child.evaluate(cache=cache)
            index_columns = [
                Column(col, name=name)
                for col, name in zip(
                    plc.reshape.tile(df.select(indices).table, npiv).columns(),
                    indices,
                    strict=True,
                )
            ]
            (variable_column,) = plc.filling.repeat(
                plc.Table(
                    [
                        plc.interop.from_arrow(
                            pa.array(
                                pivotees,
                                type=plc.interop.to_arrow(self.schema[variable_name]),
                            ),
                        )
                    ]
                ),
                df.num_rows,
            ).columns()
            value_column = plc.concatenate.concatenate(
                [
                    df.column_map[pivotee].astype(self.schema[value_name]).obj
                    for pivotee in pivotees
                ]
            )
            return DataFrame(
                [
                    *index_columns,
                    Column(variable_column, name=variable_name),
                    Column(value_column, name=value_name),
                ]
            )
        else:
            raise AssertionError("Should never be reached")  # pragma: no cover


class Union(IR):
    """Concatenate dataframes vertically."""

    __slots__ = ("zlice",)
    _non_child = ("schema", "zlice")
    zlice: tuple[int, int] | None
    """Optional slice to apply to the result."""

    def __init__(self, schema: Schema, zlice: tuple[int, int] | None, *children: IR):
        self.schema = schema
        self.zlice = zlice
        self.children = children
        schema = self.children[0].schema
        if not all(s.schema == schema for s in self.children[1:]):
            raise NotImplementedError("Schema mismatch")

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        # TODO: only evaluate what we need if we have a slice
        dfs = [df.evaluate(cache=cache) for df in self.children]
        return DataFrame.from_table(
            plc.concatenate.concatenate([df.table for df in dfs]), dfs[0].column_names
        ).slice(self.zlice)


class HConcat(IR):
    """Concatenate dataframes horizontally."""

    __slots__ = ()
    _non_child = ("schema",)

    def __init__(self, schema: Schema, *children: IR):
        self.schema = schema
        self.children = children

    @staticmethod
    def _extend_with_nulls(table: plc.Table, *, nrows: int) -> plc.Table:
        """
        Extend a table with nulls.

        Parameters
        ----------
        table
            Table to extend
        nrows
            Number of additional rows

        Returns
        -------
        New pylibcudf table.
        """
        return plc.concatenate.concatenate(
            [
                table,
                plc.Table(
                    [
                        plc.Column.all_null_like(column, nrows)
                        for column in table.columns()
                    ]
                ),
            ]
        )

    def evaluate(self, *, cache: MutableMapping[int, DataFrame]) -> DataFrame:
        """Evaluate and return a dataframe."""
        dfs = [df.evaluate(cache=cache) for df in self.children]
        max_rows = max(df.num_rows for df in dfs)
        # Horizontal concatenation extends shorter tables with nulls
        dfs = [
            df
            if df.num_rows == max_rows
            else DataFrame.from_table(
                self._extend_with_nulls(df.table, nrows=max_rows - df.num_rows),
                df.column_names,
            )
            for df in dfs
        ]
        return DataFrame(itertools.chain.from_iterable(df.columns for df in dfs))
