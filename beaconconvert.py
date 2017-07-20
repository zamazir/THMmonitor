def interpretDS18B20(raw_value):
    return float(((raw_value)/16.0))

def interpretEMC1701(raw_value):
    return float(((int(raw_value)>>5)*0.125))

def interpretLT55599(raw_value):
    return float((((raw_value - 3)*10)))

def interpretMCP9802(raw_value):
    return float(((int(raw_value)>>4)*0.0625))

def interpretUnknownCOMSensor(raw_value):
    return float(((raw_value * 1000 / 1140)))

def interpretKelvinToCelsius(raw_value):
    return float(((raw_value - 27315) /100.0))

def interpretBMX055(raw_value):
    return float((((int(raw_value) & 0xFF)/2) + 23))

def interpretNoChange(raw_value):
    return float((raw_value / 100.0))

def statusConverter(value):
    try:
        values = ["OK", "WARNING", "ALARM"]
        return values[value]
    except:
        return "UNKNOWN THM STATUS (" + str(value) + ")"
