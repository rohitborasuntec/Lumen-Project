import os
import pandas as pd
from datetime import datetime
from src.logging import logger


class File:
    
    def __init__(self,file_path,file_type):
        self.file_path = file_path
        self.file_type = file_type

    def read_file(self):
        if self.file_type == "excel":
            return pd.read_excel(self.file_type)
        elif self.file_type == "csv":
            return pd.read_csv(self.file_type)
        raise Exception("File Type Shold be either Excel or CSV")
    
    def check_if_completed(self, product, comparing_product):
        if not os.path.exists(self.file_name):
            return False

        existing = pd.read_excel(self.file_name)

        # completed_data = existing[(existing["Date"] == self.date) & (existing["Time"] == self.period) & (existing["Status"] == "Done") & (existing["First product"] == product) & (existing["Second product"] == comparing_product)]

        if completed_data.empty:
            return False

        return True
        
    def save_excel(self, row=None, df=None):
        

        logger.info(f"{self.file_name} has been saved")


if __name__ == "__main__":
    File()