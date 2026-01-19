import os
from pymongo import MongoClient

def get_db():
    mongo_uri = os.getenv("MONGO_URI")
    db_name = os.getenv("MONGO_DB")
    client = MongoClient(mongo_uri)
    return client[db_name]
