import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client


def find_duplicate_values(client, index_pattern, field_name):
    """Find duplicate values of a field across indices matching the index pattern."""
    lib.logging.define("delete-duplicates")
    logging.info(f"Searching for duplicate values in field '{field_name}' across indices '{index_pattern}'.")

    # Build the aggregation query
    agg_query = {
        "size": 0,
        "aggs": {
            "duplicate_values": {
                "terms": {
                    "field": field_name,
                    "min_doc_count": 2,
                    "size": 10000,  # Adjust based on your data size
                }
            }
        },
    }
    try:
        # Perform the search
        response = client.search(index=index_pattern, body=agg_query)
        # Extract the duplicate values
        duplicate_values = [bucket["key"] for bucket in response["aggregations"]["duplicate_values"]["buckets"]]
        logging.info(f"Found {len(duplicate_values)} duplicate values for field '{field_name}'.")
        return duplicate_values
    except Exception as e:
        logging.error(f"Error finding duplicate values: {e}")
        return []


def process_duplicates(client, index_pattern, field_name, duplicate_values):
    """Process each duplicate value to delete duplicates and keep one document."""
    lib.logging.define("delete-duplicates")
    for value in duplicate_values:
        logging.info(f"Processing duplicate value: {value}")

        # Retrieve all documents with this duplicate value
        search_query = {
            "query": {"term": {field_name: value}},
            "sort": [{"created_at": "asc"}],  # Adjust the field used for sorting as needed
            "_source": False,  # We don't need the source, only the IDs
            "size": 1000,  # Adjust if you expect more than 1000 duplicates
        }
        try:
            # Perform the search
            response = client.search(index=index_pattern, body=search_query)
            hits = response["hits"]["hits"]
            if not hits:
                logging.info(f"No documents found for value '{value}'.")
                continue

            # Decide which document to keep (e.g., the earliest one)
            doc_to_keep = hits[0]
            doc_to_keep_id = doc_to_keep["_id"]
            logging.info(f"Keeping document with ID '{doc_to_keep_id}' for value '{value}'.")

            # Collect IDs of documents to delete
            ids_to_delete = [doc["_id"] for doc in hits[1:]]
            if not ids_to_delete:
                logging.info(f"No duplicates to delete for value '{value}'.")
                continue

            # Delete duplicates using delete_by_query
            delete_query = {
                "query": {
                    "bool": {
                        "must": [{"term": {field_name: value}}],
                        "must_not": [{"ids": {"values": [doc_to_keep_id]}}],
                    }
                }
            }
            delete_response = client.delete_by_query(index=index_pattern, body=delete_query)
            deleted = delete_response.get("deleted", 0)
            logging.info(f"Deleted {deleted} duplicate documents for value '{value}'.")
        except Exception as e:
            logging.error(f"Error processing duplicates for value '{value}': {e}")


def main(params):
    lib.logging.define("delete-duplicates")  # Define logging prefix for this script
    logging.info("Starting duplicate removal process.")

    # Elasticsearch client
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    # Index pattern and field name
    index_pattern = "*echo_conversations"
    field_name = "chat_id"

    # Step 1: Find duplicate values
    duplicate_values = find_duplicate_values(es, index_pattern, field_name)

    # Step 2: Process duplicates
    process_duplicates(es, index_pattern, field_name, duplicate_values)

    logging.info("Completed duplicate removal process.")
