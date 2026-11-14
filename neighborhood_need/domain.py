from .validation import require_tables, unique, finite, references


SCHEMA = {'surveys': {'response': ['str'], 'wave': ['str'], 'area': ['str'], 'age': ['str'], 'living': ['str'], 'mobility': ['str'], 'need': ['number'], 'weight': ['number']}, 'population': {'cell': ['str'], 'boundary': ['str'], 'area': ['str'], 'age': ['str'], 'living': ['str'], 'mobility': ['str'], 'count': ['null', 'number'], 'cell_status': ['str']}, 'crosswalk': {'mapping': ['str'], 'old_area': ['str'], 'new_area': ['str'], 'low': ['number'], 'high': ['number']}, 'controls': {'wave': ['str'], 'total_need': ['null'], 'population_version': ['str']}}
OPTIONS = {'wave': ['spring', 'autumn'], 'boundary': ['old', 'new']}


def validate_structure(data, schema):
    require_tables(data, list(schema))
    for table, fields in schema.items():
        for row in data[table]:
            if not isinstance(row, dict) or any(field not in row for field in fields):
                raise ValueError("表字段缺失：" + table)
            for field, kinds in fields.items():
                value = row[field]
                kind = "null" if value is None else ("boolean" if isinstance(value,bool) else ("number" if isinstance(value,(int,float)) else type(value).__name__))
                if kind not in kinds:
                    raise ValueError("字段类型错误：" + table + "." + field)
                if kind == "number":
                    finite(value)


def validate(data):
    validate_structure(data, SCHEMA)
    unique(data['surveys'], ['response'])
    unique(data['population'], ['cell'])
    unique(data['crosswalk'], ['mapping'])
    unique(data['controls'], ['wave'])
    for row in data["population"]:
        if row["cell_status"] not in {"impossible","unknown","observed"}:
            raise ValueError("人口单元状态无效")
        if (row["cell_status"] == "unknown") != (row["count"] is None):
            raise ValueError("缺口标记与人口数不一致")
        if row["cell_status"] == "impossible" and row["count"] != 0:
            raise ValueError("结构零不能为正人口")


def view(rows, measure, note):
    return {"summary":{"明细条数":len(rows),"有数值记录":sum(isinstance(row.get(measure),(int,float)) for row in rows)},
            "rows":rows,"series":[{"label":str(position+1),"y":row.get(measure)} for position,row in enumerate(rows)],
            "notes":[note]}


def explore(data, selection):
    for key, choices in OPTIONS.items():
        if selection.get(key) not in choices:
            raise ValueError("选择范围无效：" + key)
    rows = []
    for cell in data["population"]:
        if cell["boundary"] != selection["boundary"]:
            continue
        sample = [row for row in data["surveys"] if row["wave"] == selection["wave"] and all(row[key] == cell[key] for key in ("area","age","living","mobility"))]
        rows.append({**cell,"sample_n":len(sample),"raw_need_share":sum(row["need"] for row in sample)/len(sample) if sample else None})
    return view(rows, "sample_n", "仅按原编码并排查看样本，不是新边界需求估计，空样本单元没有直接比例。")
