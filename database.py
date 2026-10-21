import mysql.connector


def get_connection():

    connection = mysql.connector.connect(
        host="localhost",
        user="root",
        password="Gauri@123",
        database="e_commerce"
    )

    return connection
