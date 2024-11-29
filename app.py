from flask import Flask, jsonify, Response
from flask_cors import CORS
from flask_caching import Cache
import gzip
import schedule
import threading
import time as time_module
from google.oauth2 import service_account
import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping, Point
from typing import Dict, Any, List, Union, Optional
import json
from datetime import datetime, time, timedelta
import numpy as np
import gspread
from gspread_dataframe import get_as_dataframe
import os

app = Flask(__name__)
CORS(app)
cache = Cache(app, config={
    'CACHE_TYPE': 'simple',
    'CACHE_DEFAULT_TIMEOUT': 60
})

latest_odor_geojson = None
last_update_time = None

REQUIRED_COLUMNS = [
    'תאריך שליחת הדיווח',
    'שעת שליחת הדיווח',
    'סוג דיווח',
    'קואורדינטות',
    'עוצמת הריח',
    'צבע העשן',
    'בדיקה',
    'ספאם'
]

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> str:
        if isinstance(obj, (datetime, time)):
            return obj.isoformat()
        elif isinstance(obj, timedelta):
            return str(obj)
        return super().default(obj)

def gzip_response(data: Dict[str, Any]) -> Response:
    gzip_buffer = gzip.compress(json.dumps(data, cls=CustomJSONEncoder).encode('utf-8'))
    return Response(
        gzip_buffer,
        mimetype='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Content-Encoding': 'gzip',
            'Last-Modified': last_update_time.strftime("%a, %d %b %Y %H:%M:%S GMT"),
            'Cache-Control': 'public, max-age=60'
        }
    )

def get_google_credentials() -> service_account.Credentials:
    credentials_json = os.getenv('GOOGLE_CREDENTIALS')
    if not credentials_json:
        raise ValueError("GOOGLE_CREDENTIALS environment variable not set")
    
    credentials_info = json.loads(credentials_json)
    return service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    )

def process_raw_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing columns: {missing_columns}")
    
    df = df[REQUIRED_COLUMNS].copy()
    df = df[(df['בדיקה'].fillna(0) != 1) & (df['ספאם'].fillna(0) != 1)]
    df = df[df['קואורדינטות'].notna() & ~df['קואורדינטות'].astype(str).str.contains('המיקום לא נמצא', na=False)]
    
    df['datetime'] = pd.to_datetime(
        df['תאריך שליחת הדיווח'].astype(str) + ' ' + df['שעת שליחת הדיווח'].astype(str),
        format='%d/%m/%Y %H:%M:%S',
        errors='coerce'
    )
    
    current_time = datetime.now()
    df['time_elapsed_minutes'] = (current_time - df['datetime']).dt.total_seconds() / 60
    df['time_elapsed_minutes'] = df['time_elapsed_minutes'].clip(lower=0)
    
    return df

def calculate_decay_rate(initial_intensity: float) -> float:
    return initial_intensity / 100 if initial_intensity != 0 else 0

def calculate_intensity(row: pd.Series) -> float:
    initial_intensity = float(row['עוצמת הריח'])
    time_elapsed = float(row['time_elapsed_minutes'])
    decay_rate = calculate_decay_rate(initial_intensity)
    current_intensity = max(0, initial_intensity - (decay_rate * time_elapsed))
    return round(current_intensity, 2)

def split_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df['קואורדינטות'].str.match(r'^-?\d+\.?\d*,-?\d+\.?\d*$', na=False)]
    
    coords_split = df['קואורדינטות'].str.split(',', expand=True)
    if coords_split.shape[1] >= 2:
        df['lat'] = pd.to_numeric(coords_split[0], errors='coerce')
        df['lon'] = pd.to_numeric(coords_split[1], errors='coerce')
    else:
        df['lat'] = np.nan
        df['lon'] = np.nan
    
    return df[df['lat'].notna() & df['lon'].notna()]

def randomize_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    earth_radius = 6378137
    max_distance = 300

    random_distances = np.random.uniform(0, max_distance, size=len(df))
    random_bearings = np.random.uniform(0, 360, size=len(df))
    random_bearings_rad = np.deg2rad(random_bearings)

    lat_rad = np.deg2rad(df['lat'].values)
    lon_rad = np.deg2rad(df['lon'].values)

    new_lat_rad = np.arcsin(
        np.sin(lat_rad) * np.cos(random_distances / earth_radius) +
        np.cos(lat_rad) * np.sin(random_distances / earth_radius) * np.cos(random_bearings_rad)
    )

    new_lon_rad = lon_rad + np.arctan2(
        np.sin(random_bearings_rad) * np.sin(random_distances / earth_radius) * np.cos(lat_rad),
        np.cos(random_distances / earth_radius) - np.sin(lat_rad) * np.sin(new_lat_rad)
    )

    df['lat'] = np.rad2deg(new_lat_rad)
    df['lon'] = np.rad2deg(new_lon_rad)

    return df

def create_geojson_no_buffer(gdf: gpd.GeoDataFrame) -> Dict[str, Any]:
    gdf = gdf.replace({np.nan: None})
    features: List[Dict[str, Any]] = []
    
    for _, row in gdf.iterrows():
        geom = row['geometry']
        props = row.drop('geometry').to_dict()
        geom_json = mapping(geom)
        feature = {
            "type": "Feature",
            "geometry": geom_json,
            "properties": props
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features
    }

def update_data() -> None:
    global latest_odor_geojson, last_update_time
    
    try:
        creds = get_google_credentials()
        gc = gspread.authorize(creds)
        
        sheet_url = "https://docs.google.com/spreadsheets/d/1PMm_4Xkrv4Bmy7p9pI8Smnqzl12xgBVotYBEb2O45cg"
        spreadsheet = gc.open_by_url(sheet_url)
        worksheet = spreadsheet.worksheet('Sheet1')
        
        df_original = process_raw_dataframe(get_as_dataframe(worksheet))
        
        odor_df = df_original[df_original['סוג דיווח'] == 'מפגע ריח'].copy()
        waste_df = df_original[df_original['סוג דיווח'] == 'מפגע פסולת'].copy()
        
        valid_smoke_colors = ['לבן', 'אפור', 'שחור']
        valid_waste_df = waste_df[
            waste_df['צבע העשן'].isin(valid_smoke_colors) &
            waste_df['צבע העשן'].notna() &
            (waste_df['צבע העשן'] != 'אין עשן')
        ].copy()
        
        valid_waste_df['עוצמת הריח'] = 6
        
        odor_df = split_coordinates(odor_df)
        valid_waste_df = split_coordinates(valid_waste_df)
        
        odor_df['עוצמת הריח'] = pd.to_numeric(odor_df['עוצמת הריח'], errors='coerce').fillna(0)
        odor_df['intensity'] = odor_df.apply(calculate_intensity, axis=1)
        
        valid_waste_df['intensity'] = valid_waste_df.apply(calculate_intensity, axis=1)
        
        odor_df = randomize_coordinates(odor_df)
        
        combined_df = pd.concat([odor_df, valid_waste_df])
        combined_df = combined_df[combined_df['intensity'] > 0]
        
        if len(combined_df) > 0:
            combined_gdf = gpd.GeoDataFrame(
                combined_df,
                geometry=gpd.points_from_xy(combined_df['lon'], combined_df['lat']),
                crs='EPSG:4326'
            )
        else:
            combined_gdf = gpd.GeoDataFrame(
                columns=combined_df.columns.tolist() + ['geometry'],
                crs='EPSG:4326'
            )
        
        latest_odor_geojson = create_geojson_no_buffer(combined_gdf)
        last_update_time = datetime.now()
        
        cache.delete('odor_data')
        
    except Exception as e:
        print(f"[{datetime.now()}] ERROR updating data: {str(e)}")
        print("Error details:", e.__class__.__name__)
        import traceback
        print(traceback.format_exc())

@app.route('/')
def home() -> Dict[str, Any]:
    return jsonify({
        "status": "running",
        "last_update": last_update_time.isoformat() if last_update_time else None,
        "endpoints": {
            "odor": "/odor",
            "status": "/status"
        }
    })

@app.route('/odor')
@cache.cached(timeout=60, key_prefix='odor_data')
def serve_odor_geojson() -> Union[Response, tuple]:
    if latest_odor_geojson is None:
        return jsonify({"error": "No data available"}), 503
    
    return gzip_response(latest_odor_geojson)

@app.route('/status')
def server_status() -> Dict[str, Any]:
    return jsonify({
        "status": "running",
        "last_update": last_update_time.isoformat() if last_update_time else None,
        "has_odor_data": latest_odor_geojson is not None
    })

def run_schedule() -> None:
    while True:
        schedule.run_pending()
        time_module.sleep(1)

def initialize_app() -> None:
    try:
        update_data()
        schedule.every(1).minutes.do(update_data)
        scheduler_thread = threading.Thread(target=run_schedule)
        scheduler_thread.daemon = True
        scheduler_thread.start()
        
    except Exception as e:
        print(f"Error during initialization: {str(e)}")
        import traceback
        print(traceback.format_exc())

initialize_app()

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port)