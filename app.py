import io
import json
import os
import tempfile
from datetime import datetime
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), "work", ".mplconfig"))
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

import pandas as pd
import streamlit as st

try:
    from inference_sdk import InferenceHTTPClient
except ImportError:
    InferenceHTTPClient = None


WORKSPACE_NAME = "bilols-workspace-wcafe"
WORKFLOW_ID = "text-recognition"
ROBOFLOW_API_URL = "https://serverless.roboflow.com"
RECEIPT_COLUMNS = [
    "merchant",
    "date",
    "subtotal",
    "tax",
    "total",
    "currency",
    "payment_method",
    "category",
    "line_items",
]
SCALAR_KEYS = ("value", "text", "raw", "normalized", "amount")
FIELD_NAME_KEYS = ("field", "field_name", "name", "label", "key")
FIELD_VALUE_KEYS = ("value", "text", "amount", "raw", "normalized")


st.set_page_config(
    page_title="Receipt Capture",
    layout="wide",
)


def get_secret(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]

    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in SCALAR_KEYS:
            if key in value and value[key] is not None:
                return normalize_scalar(value[key])
    return str(value)


def flatten_dict(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    row: dict[str, str] = {}
    for key, value in data.items():
        label = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            if any(field_key in value for field_key in SCALAR_KEYS):
                row[label] = normalize_scalar(value)
            else:
                nested = flatten_dict(value, label)
                row.update(nested)
        elif isinstance(value, list):
            row[label] = "; ".join(normalize_scalar(item) for item in value)
        else:
            row[label] = normalize_scalar(value)
    return row


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if not text or text[0] not in "[{":
        return value

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def find_first_key(data: Any, target_key: str) -> Any:
    data = parse_jsonish(data)

    if isinstance(data, dict):
        if target_key in data:
            return data[target_key]
        for value in data.values():
            found = find_first_key(value, target_key)
            if found is not None:
                return found

    if isinstance(data, list):
        for item in data:
            found = find_first_key(item, target_key)
            if found is not None:
                return found

    return None


def compact_raw_output(data: Any) -> pd.DataFrame:
    data = parse_jsonish(data)
    if isinstance(data, dict):
        return pd.DataFrame([flatten_dict(data)])
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return pd.DataFrame([flatten_dict(item) for item in data])
    if isinstance(data, list):
        return pd.DataFrame({"value": [normalize_scalar(item) for item in data]})
    if data is None:
        return pd.DataFrame()
    return pd.DataFrame({"value": [normalize_scalar(data)]})


def format_line_items(value: Any) -> str:
    value = parse_jsonish(value)
    if not value:
        return ""

    if isinstance(value, list):
        formatted_items = []
        for item in value:
            if isinstance(item, dict):
                parts = [f"{key}: {normalize_scalar(val)}" for key, val in item.items() if val not in (None, "")]
                formatted_items.append(", ".join(parts))
            else:
                formatted_items.append(normalize_scalar(item))
        return "; ".join(item for item in formatted_items if item)

    return normalize_scalar(value)


def line_items_to_dataframe(value: Any) -> pd.DataFrame:
    value = parse_jsonish(value)
    if not value:
        return pd.DataFrame()

    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return pd.DataFrame(value)

    if isinstance(value, list):
        return pd.DataFrame({"item": [normalize_scalar(item) for item in value]})

    return pd.DataFrame({"line_items": [normalize_scalar(value)]})


def order_receipt_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    ordered = [column for column in RECEIPT_COLUMNS if column in df.columns]
    extras = [column for column in df.columns if column not in ordered]
    return df[ordered + extras]


def receipt_dict_to_row(data: dict[str, Any]) -> dict[str, str]:
    row = {}
    for column in RECEIPT_COLUMNS:
        value = data.get(column, "")
        row[column] = format_line_items(value) if column == "line_items" else normalize_scalar(value)

    for key, value in data.items():
        if key not in row:
            row[key] = format_line_items(value) if key == "line_items" else normalize_scalar(value)

    return row


def field_record_to_pair(record: dict[str, Any]) -> tuple[str, str] | None:
    name = None
    value = None

    for key in FIELD_NAME_KEYS:
        if key in record and record[key]:
            name = normalize_scalar(record[key])
            break

    for key in FIELD_VALUE_KEYS:
        if key in record and record[key] is not None:
            value = normalize_scalar(record[key])
            break

    if name and value is not None:
        return name, value
    return None


def parse_receipt_csv(receipt_csv: Any) -> pd.DataFrame:
    receipt_csv = parse_jsonish(receipt_csv)

    if receipt_csv is None:
        return pd.DataFrame()

    if isinstance(receipt_csv, pd.DataFrame):
        return receipt_csv

    if isinstance(receipt_csv, list):
        if not receipt_csv:
            return pd.DataFrame()
        if all(isinstance(item, dict) for item in receipt_csv):
            return order_receipt_columns(pd.DataFrame(receipt_csv))
        return pd.DataFrame({"value": receipt_csv})

    if isinstance(receipt_csv, dict):
        return order_receipt_columns(pd.DataFrame([receipt_dict_to_row(receipt_csv)]))

    text = str(receipt_csv).strip()
    if not text:
        return pd.DataFrame()

    try:
        return order_receipt_columns(pd.read_csv(io.StringIO(text)))
    except Exception:
        return pd.DataFrame({"receipt_csv": [text]})


def parse_receipt_fields(fields: Any) -> pd.DataFrame:
    fields = parse_jsonish(fields)

    if fields is None:
        return pd.DataFrame()

    if isinstance(fields, dict):
        return order_receipt_columns(pd.DataFrame([receipt_dict_to_row(fields)]))

    if isinstance(fields, list):
        paired_fields = [field_record_to_pair(item) for item in fields if isinstance(item, dict)]
        paired_fields = [item for item in paired_fields if item is not None]
        if paired_fields:
            return order_receipt_columns(pd.DataFrame([receipt_dict_to_row(dict(paired_fields))]))

        if all(isinstance(item, dict) for item in fields):
            return order_receipt_columns(pd.DataFrame([receipt_dict_to_row(item) for item in fields]))
        return pd.DataFrame({"field": [normalize_scalar(item) for item in fields]})

    return pd.DataFrame({"fields": [normalize_scalar(fields)]})


def call_roboflow(image_path: str, api_key: str) -> dict[str, Any]:
    if InferenceHTTPClient is None:
        raise RuntimeError(
            "The inference-sdk package is not installed. Run `pip install -r requirements.txt`."
        )

    client = InferenceHTTPClient(api_url=ROBOFLOW_API_URL, api_key=api_key)
    result = client.run_workflow(
        workspace_name=WORKSPACE_NAME,
        workflow_id=WORKFLOW_ID,
        images={"image": image_path},
        use_cache=True,
    )

    if not result:
        raise RuntimeError("Roboflow returned an empty result.")

    output = result[0]
    receipt_fields = find_first_key(output, "receipt_fields")
    receipt_csv = find_first_key(output, "receipt_csv")
    raw_model_output = find_first_key(output, "model_output")

    if receipt_fields is None:
        receipt_fields = find_first_key(raw_model_output, "receipt_fields")
    if receipt_csv is None:
        receipt_csv = find_first_key(raw_model_output, "receipt_csv")

    return {
        "fields": receipt_fields,
        "csv": receipt_csv,
        "raw_model_output": raw_model_output,
        "raw_output": output,
    }


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Receipts")
    return buffer.getvalue()


def save_upload_to_temp(uploaded_file: Any) -> str:
    suffix = os.path.splitext(uploaded_file.name)[1] or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(uploaded_file.getbuffer())
        return temp_file.name


def add_metadata(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df.insert(0, "source_file", source_file)
    df.insert(1, "processed_at", datetime.now().isoformat(timespec="seconds"))
    return df


def show_key_value_view(df: pd.DataFrame) -> None:
    for index, row in df.iterrows():
        source_file = row.get("source_file", f"Receipt {index + 1}")
        st.subheader(str(source_file))
        display_row = row.drop(labels=["source_file", "processed_at"], errors="ignore")
        display_row = display_row[display_row.astype(str).str.len() > 0]
        kv_df = pd.DataFrame(
            {
                "Field": display_row.index,
                "Value": display_row.astype(str).values,
            }
        )
        st.dataframe(kv_df, width="stretch", hide_index=True)


def show_receipt_view(df: pd.DataFrame, raw_outputs: list[dict[str, Any]]) -> None:
    for index, row in df.iterrows():
        source_file = row.get("source_file", f"Receipt {index + 1}")
        st.subheader(str(source_file))

        merchant = row.get("merchant", "")
        date = row.get("date", "")
        currency = row.get("currency", "")
        total = row.get("total", "")
        subtotal = row.get("subtotal", "")
        tax = row.get("tax", "")
        payment_method = row.get("payment_method", "")
        category = row.get("category", "")

        top_cols = st.columns(4)
        top_cols[0].metric("Merchant", merchant or "unknown")
        top_cols[1].metric("Date", date or "unknown")
        total_label = f"{currency} {total}".strip() or "unknown"
        top_cols[2].metric("Total", total_label)
        top_cols[3].metric("Category", category or "other")

        details_df = pd.DataFrame(
            [
                {"Field": "Subtotal", "Value": subtotal},
                {"Field": "Tax", "Value": tax},
                {"Field": "Payment method", "Value": payment_method},
                {"Field": "Currency", "Value": currency},
            ]
        )
        details_df = details_df[details_df["Value"].astype(str).str.len() > 0]
        if not details_df.empty:
            st.dataframe(details_df, width="stretch", hide_index=True)

        raw_line_items = None
        for raw_output in raw_outputs:
            if raw_output.get("source_file") == source_file:
                raw_line_items = find_first_key(raw_output.get("fields"), "line_items")
                break

        line_items_df = line_items_to_dataframe(raw_line_items if raw_line_items is not None else row.get("line_items", ""))
        if not line_items_df.empty:
            st.write("Line items")
            st.dataframe(line_items_df, width="stretch", hide_index=True)


def main() -> None:
    st.title("Receipt Capture")
    st.caption("Upload receipt photos, extract the useful fields, then download CSV or Excel-ready data.")

    with st.sidebar:
        st.header("Roboflow")
        api_key = st.text_input(
            "API key",
            value=get_secret("ROBOFLOW_API_KEY"),
            type="password",
            help="Set ROBOFLOW_API_KEY in your environment or paste it here for the demo.",
        )
        show_raw = st.toggle("Show raw model output", value=False)
        st.divider()
        st.write("Workspace")
        st.code(WORKSPACE_NAME, language=None)
        st.write("Workflow")
        st.code(WORKFLOW_ID, language=None)

    uploaded_files = st.file_uploader(
        "Upload receipt images",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )

    if InferenceHTTPClient is None:
        st.warning("`inference-sdk` is not installed yet. Install dependencies before extraction.")

    if not uploaded_files:
        st.info("Add one or more receipt images to start.")
        return

    preview_cols = st.columns(min(3, len(uploaded_files)))
    for index, uploaded_file in enumerate(uploaded_files):
        with preview_cols[index % len(preview_cols)]:
            st.image(uploaded_file, caption=uploaded_file.name, width="stretch")

    if not api_key:
        st.error("Add your Roboflow API key in the sidebar to extract receipt data.")
        return

    if st.button("Extract receipts", type="primary", width="stretch"):
        all_rows: list[pd.DataFrame] = []
        field_rows: list[pd.DataFrame] = []
        fallback_rows: list[pd.DataFrame] = []
        raw_outputs: list[dict[str, Any]] = []

        progress = st.progress(0)
        status = st.empty()

        for index, uploaded_file in enumerate(uploaded_files, start=1):
            status.write(f"Processing {uploaded_file.name}...")
            image_path = save_upload_to_temp(uploaded_file)

            try:
                extracted = call_roboflow(image_path, api_key)
                csv_df = parse_receipt_csv(extracted["csv"])
                fields_df = parse_receipt_fields(extracted["fields"])
                fallback_df = compact_raw_output(extracted["raw_model_output"])
                if fallback_df.empty:
                    fallback_df = compact_raw_output(extracted["raw_output"])

                csv_df = add_metadata(csv_df, uploaded_file.name)
                fields_df = add_metadata(fields_df, uploaded_file.name)
                fallback_df = add_metadata(fallback_df, uploaded_file.name)

                if not csv_df.empty:
                    all_rows.append(csv_df)
                if not fields_df.empty:
                    field_rows.append(fields_df)
                if not fallback_df.empty:
                    fallback_rows.append(fallback_df)

                raw_outputs.append(
                    {
                        "source_file": uploaded_file.name,
                        "fields": extracted["fields"],
                        "csv": extracted["csv"],
                        "raw_model_output": extracted["raw_model_output"],
                        "raw_output": extracted["raw_output"],
                    }
                )
            except Exception as exc:
                st.error(f"{uploaded_file.name}: {exc}")
            finally:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass

            progress.progress(index / len(uploaded_files))

        status.success("Extraction complete. Review the formatted results below.")

        csv_results = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
        field_results = pd.concat(field_rows, ignore_index=True) if field_rows else pd.DataFrame()
        fallback_results = pd.concat(fallback_rows, ignore_index=True) if fallback_rows else pd.DataFrame()

        st.session_state["csv_results"] = csv_results
        st.session_state["field_results"] = field_results
        st.session_state["fallback_results"] = fallback_results
        st.session_state["raw_outputs"] = raw_outputs

    csv_results = st.session_state.get("csv_results", pd.DataFrame())
    field_results = st.session_state.get("field_results", pd.DataFrame())
    fallback_results = st.session_state.get("fallback_results", pd.DataFrame())
    raw_outputs = st.session_state.get("raw_outputs", [])

    if fallback_results.empty and raw_outputs:
        fallback_rows = []
        for raw_output in raw_outputs:
            source_file = raw_output.get("source_file", "receipt")
            fallback_df = compact_raw_output(raw_output.get("raw_model_output"))
            if fallback_df.empty:
                fallback_df = compact_raw_output(raw_output.get("raw_output"))
            fallback_df = add_metadata(fallback_df, source_file)
            if not fallback_df.empty:
                fallback_rows.append(fallback_df)
        fallback_results = pd.concat(fallback_rows, ignore_index=True) if fallback_rows else pd.DataFrame()
        st.session_state["fallback_results"] = fallback_results

    if csv_results.empty and field_results.empty and fallback_results.empty and not raw_outputs:
        return

    st.header("Receipt data")
    if csv_results.empty and field_results.empty and not fallback_results.empty:
        st.warning(
            "Roboflow responded, but the app did not find `receipt_fields` or `receipt_csv`. "
            "Showing a parsed fallback from the model response."
        )

    tab_summary, tab_csv, tab_fields, tab_fallback, tab_raw = st.tabs(
        ["Clear view", "Spreadsheet output", "Extracted fields", "Parsed fallback", "Raw"]
    )

    with tab_summary:
        if not field_results.empty:
            show_receipt_view(field_results, raw_outputs)
        elif not csv_results.empty:
            st.dataframe(csv_results, width="stretch", hide_index=True)
        elif not fallback_results.empty:
            show_key_value_view(fallback_results)
        else:
            st.info("No readable receipt data was returned. Turn on raw output in the sidebar to inspect the response.")

    with tab_csv:
        if csv_results.empty:
            st.info("No CSV-style rows were returned. Check the extracted fields tab.")
        else:
            st.dataframe(csv_results, width="stretch", hide_index=True)
            csv_col, xlsx_col = st.columns(2)
            with csv_col:
                st.download_button(
                    "Download CSV",
                    data=dataframe_to_csv_bytes(csv_results),
                    file_name="receipt_output.csv",
                    mime="text/csv",
                    width="stretch",
                )
            with xlsx_col:
                st.download_button(
                    "Download Excel",
                    data=dataframe_to_excel_bytes(csv_results),
                    file_name="receipt_output.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
            )

    with tab_fields:
        if field_results.empty:
            st.info("No structured fields were returned.")
        else:
            st.dataframe(field_results, width="stretch", hide_index=True)
            st.download_button(
                "Download fields CSV",
                data=dataframe_to_csv_bytes(field_results),
                file_name="receipt_fields.csv",
                mime="text/csv",
                width="stretch",
            )

    with tab_fallback:
        if fallback_results.empty:
            st.info("No fallback rows were needed.")
        else:
            st.dataframe(fallback_results, width="stretch", hide_index=True)
            st.download_button(
                "Download fallback CSV",
                data=dataframe_to_csv_bytes(fallback_results),
                file_name="receipt_fallback.csv",
                mime="text/csv",
                width="stretch",
            )

    with tab_raw:
        if show_raw:
            st.json(raw_outputs)
        else:
            st.info("Turn on raw output in the sidebar to inspect the full Roboflow response.")


if __name__ == "__main__":
    main()
