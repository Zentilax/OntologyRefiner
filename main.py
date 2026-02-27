import io
import os
import sys
import tempfile
import traceback
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import owlready2
from owlready2 import get_ontology, ThingClass, destroy_entity

app = FastAPI(title="Ontology Refiner API", version="1.0.0")

# ── CORS — allow GitHub Pages frontend ────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "*"          # tighten to your GH Pages URL in production env var
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_onto(owl_bytes: bytes):
    """Load OWL bytes into owlready2 and return the ontology object."""
    with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as tmp:
        tmp.write(owl_bytes)
        tmp_path = tmp.name
    try:
        uri = "file://" + tmp_path
        onto = get_ontology(uri).load()
    finally:
        os.unlink(tmp_path)
    return onto


def save_onto(onto) -> bytes:
    """Serialise ontology to bytes (RDF/XML)."""
    with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        onto.save(file=tmp_path, format="rdfxml")
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def get_label(e):
    lbs = list(e.label)
    return lbs[0] if lbs else ""


def get_comment(e):
    cm = list(e.comment)
    return cm[0] if cm else ""


def find_class(onto, name: str):
    nl = name.lower()
    for c in onto.classes():
        if c.name.lower() == nl or get_label(c).lower() == nl:
            return c
    return None


def find_individual(onto, name: str):
    nl = name.lower()
    for i in onto.individuals():
        if i.name.lower() == nl:
            return i
    return None


def find_property(onto, name: str):
    nl = name.lower()
    for p in list(onto.object_properties()) + list(onto.data_properties()):
        if p.name.lower() == nl:
            return p
    return None


def fmt_list(lst):
    return [x.name if hasattr(x, "name") else str(x) for x in lst]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Ontology Refiner API"}


@app.post("/parse")
async def parse_ontology(file: UploadFile = File(...)):
    """
    Upload an OWL file. Returns classes, individuals, and properties as JSON.
    The raw OWL bytes are returned as base64 so the frontend can hold them
    in memory and re-send for each operation (no server-side state / volumes).
    """
    import base64
    try:
        owl_bytes = await file.read()
        onto = load_onto(owl_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load ontology: {e}")

    # ── classes ──────────────────────────────────────────────────────────────
    classes = []
    for c in onto.classes():
        classes.append({
            "name": c.name,
            "label": get_label(c),
            "comment": get_comment(c),
        })

    # ── individuals ──────────────────────────────────────────────────────────
    individuals = []
    for ind in onto.individuals():
        types = [cls.name for cls in ind.is_a if isinstance(cls, ThingClass)]
        props = {}
        for prop in onto.object_properties():
            vals = getattr(ind, prop.name, [])
            if not hasattr(vals, "__iter__"):
                vals = [vals] if vals else []
            v_names = [v.name for v in vals if v is not None]
            if v_names:
                props[prop.name] = v_names
        data_props = {}
        for prop in onto.data_properties():
            vals = getattr(ind, prop.name, [])
            if not hasattr(vals, "__iter__"):
                vals = [vals] if vals else []
            v_vals = [str(v) for v in vals if v is not None]
            if v_vals:
                data_props[prop.name] = v_vals
        individuals.append({
            "name": ind.name,
            "types": types,
            "object_properties": props,
            "data_properties": data_props,
        })

    # ── properties ───────────────────────────────────────────────────────────
    obj_props = []
    for p in onto.object_properties():
        obj_props.append({
            "name": p.name,
            "domain": fmt_list(p.domain),
            "range": fmt_list(p.range),
            "type": "object",
        })
    data_props = []
    for p in onto.data_properties():
        data_props.append({
            "name": p.name,
            "domain": fmt_list(p.domain),
            "range": fmt_list(p.range),
            "type": "data",
        })

    owl_b64 = base64.b64encode(owl_bytes).decode()

    return {
        "classes": classes,
        "individuals": individuals,
        "object_properties": obj_props,
        "data_properties": data_props,
        "owl_b64": owl_b64,   # sent back to client; re-used in mutate calls
    }


@app.post("/mutate")
async def mutate_ontology(payload: dict):
    """
    Apply a list of operations to the ontology (provided as base64).
    Returns the refined OWL as base64 plus updated parsed data.

    payload:
      {
        "owl_b64": "<base64 OWL>",
        "operations": [
          {"op": "rename_class",       "target": "OldName",  "new_name": "NewName"},
          {"op": "set_class_label",    "target": "ClassName","value": "New Label"},
          {"op": "set_class_comment",  "target": "ClassName","value": "New Comment"},
          {"op": "rename_individual",  "target": "OldName",  "new_name": "NewName"},
          {"op": "delete_individual",  "target": "IndName"},
          {"op": "add_individual",     "class_name": "CName","name": "IndName"},
          {"op": "add_assertion",      "subject": "Ind","property": "Prop","object": "Ind2"},
          {"op": "delete_assertion",   "subject": "Ind","property": "Prop","object": "Ind2"},
          {"op": "rename_property",    "target": "OldProp",  "new_name": "NewProp"},
          {"op": "set_prop_domain",    "target": "Prop",     "class_name": "Class"},
          {"op": "set_prop_range_class","target":"Prop",     "class_name": "Class"},
          {"op": "set_prop_range_type","target": "Prop",     "range_type": "str|int|float|bool"},
          {"op": "set_data_value",     "subject": "Ind",     "property": "Prop", "value": "val", "value_type": "str|int|float|bool"},
          {"op": "delete_data_value",  "subject": "Ind",     "property": "Prop"},
        ]
      }
    """
    import base64

    owl_b64 = payload.get("owl_b64")
    operations = payload.get("operations", [])

    if not owl_b64:
        raise HTTPException(status_code=400, detail="Missing owl_b64")

    try:
        owl_bytes = base64.b64decode(owl_b64)
        onto = load_onto(owl_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load ontology: {e}")

    errors = []
    applied = []

    type_map = {"str": str, "string": str, "int": int,
                "integer": int, "float": float, "bool": bool}

    with onto:
        for op_idx, op in enumerate(operations):
            op_name = op.get("op")
            try:
                if op_name == "rename_class":
                    cls = find_class(onto, op["target"])
                    if cls is None:
                        raise ValueError(f"Class '{op['target']}' not found")
                    cls.name = op["new_name"]
                    applied.append(op)

                elif op_name == "set_class_label":
                    cls = find_class(onto, op["target"])
                    if cls is None:
                        raise ValueError(f"Class '{op['target']}' not found")
                    cls.label = [op["value"]]
                    applied.append(op)

                elif op_name == "set_class_comment":
                    cls = find_class(onto, op["target"])
                    if cls is None:
                        raise ValueError(f"Class '{op['target']}' not found")
                    cls.comment = [op["value"]]
                    applied.append(op)

                elif op_name == "rename_individual":
                    ind = find_individual(onto, op["target"])
                    if ind is None:
                        raise ValueError(f"Individual '{op['target']}' not found")
                    ind.name = op["new_name"]
                    applied.append(op)

                elif op_name == "delete_individual":
                    ind = find_individual(onto, op["target"])
                    if ind is None:
                        raise ValueError(f"Individual '{op['target']}' not found")
                    destroy_entity(ind)
                    applied.append(op)

                elif op_name == "add_individual":
                    cls = find_class(onto, op["class_name"])
                    if cls is None:
                        raise ValueError(f"Class '{op['class_name']}' not found")
                    if not op["name"].strip():
                        raise ValueError("Individual name cannot be empty")
                    cls(op["name"])
                    applied.append(op)

                elif op_name == "add_assertion":
                    subj = find_individual(onto, op["subject"])
                    prop = find_property(onto, op["property"])
                    obj  = find_individual(onto, op["object"])
                    if subj is None:
                        raise ValueError(f"Individual '{op['subject']}' not found")
                    if prop is None:
                        raise ValueError(f"Property '{op['property']}' not found")
                    if obj is None:
                        raise ValueError(f"Individual '{op['object']}' not found")
                    getattr(subj, prop.name).append(obj)
                    applied.append(op)

                elif op_name == "delete_assertion":
                    subj = find_individual(onto, op["subject"])
                    prop = find_property(onto, op["property"])
                    obj  = find_individual(onto, op["object"])
                    if None in (subj, prop, obj):
                        raise ValueError("Subject, property, or object not found")
                    vals = getattr(subj, prop.name)
                    if obj in vals:
                        vals.remove(obj)
                    else:
                        raise ValueError("Assertion not found")
                    applied.append(op)

                elif op_name == "rename_property":
                    prop = find_property(onto, op["target"])
                    if prop is None:
                        raise ValueError(f"Property '{op['target']}' not found")
                    prop.name = op["new_name"]
                    applied.append(op)

                elif op_name == "set_prop_domain":
                    prop = find_property(onto, op["target"])
                    cls  = find_class(onto, op["class_name"])
                    if prop is None:
                        raise ValueError(f"Property '{op['target']}' not found")
                    if cls is None:
                        raise ValueError(f"Class '{op['class_name']}' not found")
                    prop.domain = [cls]
                    applied.append(op)

                elif op_name == "set_prop_range_class":
                    prop = find_property(onto, op["target"])
                    cls  = find_class(onto, op["class_name"])
                    if prop is None:
                        raise ValueError(f"Property '{op['target']}' not found")
                    if cls is None:
                        raise ValueError(f"Class '{op['class_name']}' not found")
                    prop.range = [cls]
                    applied.append(op)

                elif op_name == "set_prop_range_type":
                    prop = find_property(onto, op["target"])
                    if prop is None:
                        raise ValueError(f"Property '{op['target']}' not found")
                    rng_type = type_map.get(op["range_type"].lower())
                    if rng_type is None:
                        raise ValueError(f"Unknown type '{op['range_type']}'")
                    prop.range = [rng_type]
                    applied.append(op)

                elif op_name == "set_data_value":
                    # Set (overwrite) a data property value on an individual.
                    # op: { "subject": "IndName", "property": "PropName", "value": "new val", "value_type": "str|int|float|bool" }
                    subj = find_individual(onto, op["subject"])
                    prop = find_property(onto, op["property"])
                    if subj is None:
                        raise ValueError(f"Individual '{op['subject']}' not found")
                    if prop is None:
                        raise ValueError(f"Property '{op['property']}' not found")
                    raw = op.get("value", "")
                    vtype = type_map.get((op.get("value_type") or "str").lower(), str)
                    try:
                        coerced = vtype(raw)
                    except (ValueError, TypeError):
                        raise ValueError(f"Cannot convert '{raw}' to {vtype.__name__}")
                    setattr(subj, prop.name, [coerced])
                    applied.append(op)

                elif op_name == "delete_data_value":
                    # Remove all data property values for a given property on an individual.
                    # op: { "subject": "IndName", "property": "PropName" }
                    subj = find_individual(onto, op["subject"])
                    prop = find_property(onto, op["property"])
                    if subj is None:
                        raise ValueError(f"Individual '{op['subject']}' not found")
                    if prop is None:
                        raise ValueError(f"Property '{op['property']}' not found")
                    setattr(subj, prop.name, [])
                    applied.append(op)

                else:
                    errors.append({"op_index": op_idx, "op": op_name,
                                   "error": "Unknown operation"})

            except Exception as e:
                errors.append({"op_index": op_idx, "op": op_name,
                               "error": str(e)})

    refined_bytes = save_onto(onto)
    refined_b64 = base64.b64encode(refined_bytes).decode()

    return {
        "applied": len(applied),
        "errors": errors,
        "owl_b64": refined_b64,
    }


@app.post("/download")
async def download_ontology(payload: dict):
    """Convert base64 OWL back to a downloadable file."""
    import base64
    owl_b64 = payload.get("owl_b64")
    if not owl_b64:
        raise HTTPException(status_code=400, detail="Missing owl_b64")
    owl_bytes = base64.b64decode(owl_b64)
    return Response(
        content=owl_bytes,
        media_type="application/rdf+xml",
        headers={"Content-Disposition": "attachment; filename=ontology_refined.owl"},
    )


@app.post("/check-consistency")
async def check_consistency(payload: dict):
    """
    Run HermiT/Pellet reasoner on the ontology.
    Returns {"consistent": bool, "message": str, "details": [...]}
    """
    import base64
    owl_b64 = payload.get("owl_b64")
    if not owl_b64:
        raise HTTPException(status_code=400, detail="Missing owl_b64")

    try:
        owl_bytes = base64.b64decode(owl_b64)
        onto = load_onto(owl_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        with onto:
            owlready2.sync_reasoner_hermit(infer_property_values=True)
        return {"consistent": True, "message": "Ontology is consistent (HermiT).", "details": []}
    except owlready2.base.OwlReadyInconsistentOntologyError:
        bad_cls = [c.name for c in onto.classes()
                   if owlready2.Nothing in c.equivalent_to]
        bad_ind = [i.name for i in onto.individuals()
                   if owlready2.Nothing in i.is_a]
        return {"consistent": False, "message": "Ontology is INCONSISTENT (HermiT).",
                "details": {"unsatisfiable_classes": bad_cls, "inconsistent_individuals": bad_ind}}
    except Exception as e:
        # Java/HermiT not available — skip gracefully
        return {"consistent": None,
                "message": f"Reasoner unavailable: {e}. Install Java to enable consistency checks.",
                "details": []}