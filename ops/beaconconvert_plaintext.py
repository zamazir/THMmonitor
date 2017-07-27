import struct as st

def interpretDS18B20(raw_value):
    return ((st.unpack('<h',st.pack('>h',raw_value))[0])/16.0)

def interpretEMC1701(raw_value):
    return ((int(st.unpack('<h',st.pack('>h',raw_value))[0])>>5)*0.125)

def interpretLT55599(raw_value):
    return (((st.unpack('<h',st.pack('>h',raw_value))[0] - 3)*10))

def interpretMCP9802(raw_value):
    return ((int(st.unpack('<h',st.pack('>h',raw_value))[0])>>4)*0.0625)

def interpretUnknownCOMSensor(raw_value):
    return ((st.unpack('<h',st.pack('>h',raw_value))[0] * 1000 / 1140))

def interpretKelvinToCelsius(raw_value):
    return ((st.unpack('<h',st.pack('>h',raw_value))[0] - 27315) /100.0)

def interpretBMX055(raw_value):
    return (((int(st.unpack('<h',st.pack('>h',raw_value))[0]) & 0xFF)/2) + 23)

def interpretNoChange(raw_value):
    return (st.unpack('<h',st.pack('>h',raw_value))[0] / 100.0)

