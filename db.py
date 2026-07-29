import os
from pymongo import MongoClient

def get_db():
    mongo_uri = "mongodb://localhost:27017/"
    db_name = "inventory_test"
    client = MongoClient(mongo_uri)
    return client[db_name]
