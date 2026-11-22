read_register = {
  "5003": "daily_power_yield_0.01", # Wh
  "5004": "total_power_yield_100",  # MWh
  "5005": "total_power_yieldB",     # MWh
  "5008": "internal_temp_10",       # C
  #"5009": "total_apparent_powerA",   # always 65535
  #"5010": "total_apparent_powerB",   # always 65535
  "5011": "pv1_voltage_10",         # V
  "5012": "pv1_current_10",         # A
  "5013": "pv2_voltage_10",         # V
  "5014": "pv2_current_10",         # A
  "5017": "total_pv_power",         # W
  "5018": "total_pv_powerB",
  "5019": "grid_voltage_10",        # V
  "5022": "inverter_current_10",    # A
  "5031": "total_active_power",     # W
  "5032": "total_active_powerB",   
  "5035": "power_factor_1000",      # 0.001
  "5036": "grid_frequency_10",      # Hz
  
  "5038": "work_state1",
  
  "5045": "fault_code1",

  "5081": "work_state2A",
  "5082": "work_state2B",
  "5083": "export_power_overflow",  # W - House Grid Consumption (+ = importing, - = exporting) / "Meter Active Power"
  "5084": "export_power_indicator", # kW - House Grid Consumption Overflow Indicator

  "5091": "power_meter",            # W - House Overall Consumption / "Load Power"
  "5092": "power_meter2",

  "5093": "daily_export_energy_10",    # "daily reverse active energy"
  "5094": "daily_export_energyB",
  "5095": "total_export_energy_10",    # "reverse active energy"
  "5096": "total_export_energyB",

  "5097": "daily_purchased_energy_10", # kW /  daily import energy / "daily forward active energy"
  "5098": "daily_purchased_energyB",

  "5099": "total_purchased_energy_10",     # "forward active energy" 
  "5100": "total_purchased_energyB",

  "5101": "daily_energy_consumption_pv_0.01", # Wh / daily direct energy consumption / "daily load energy consumption from PV"
  "5102": "daily_energy_consumptionB",

  "5103": "total_energy_consumption_pv_10",   # kWh / total direct energy consumption / "total load energy consumption from PV"
  "5104": "total_energy_consumptionB",

  "5113": "daily_running_time",
  "5146": "negative_to_ground_voltage",
  "5150": "pid_work_state",
}

holding_register = {
  "5000": "year",
  "5001": "month",
  "5002": "day",
  "5003": "hour",
  "5004": "minute",
  "5005": "second",
  "5006": "start_stop",
  "5007": "power_limit",
  "5010": "export_limit",

}

scan = """{
    "read": [
        {
            "start": "5000",
            "range": "100"
        },
        {
            "start": "5100",
            "range": "100"
        }
  ],
  "holding": [
    {
      "start": "4999",
      "range": "12"
    }
  ]
}"""

# Match Modbus registers to pvoutput api fields
# Reference: https://pvoutput.org/help.html#api-addstatus
pvoutput = {
  "Energy Generation": "daily_power_yield",
  "Power Generation": "total_active_power",
  "Energy Consumption": "daily_energy_consumption_pv",
  #"Power Consumption": "power_meter",    # power meter is recording overall house consumption (including imported power), not just the consumption of PV so don't supply this value otherwise pvoutput.org gets confused
  "Temperature": "internal_temp",
  "Voltage": "grid_voltage"
}
