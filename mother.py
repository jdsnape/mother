import paho.mqtt.client as mqtt
import datetime
import os
import time
import json
import ConfigParser as configparser
import argparse
from collections import defaultdict
import logging
import sqlite3
from slackclient import SlackClient
from threading import Timer

#standard class which defines init
# What things are common to all monitor types?
class monitor(object):
	def __init__(self,topic, notify_message, method, name):
		self.topic = topic
		self.method = method	
		self.notify_message = notify_message
		self.name=name
		

	def on_message(self, client, userdata, message):
		logging.debug("Messaage rcvd in generic class, this shouldn't happen :)")

class state_monitor(monitor):
	def __init__(self,topic, return_state,notify_message, method, name):
		self.return_state=return_state
		self.current_state=return_state
		super(state_monitor,self).__init__(topic, notify_message,method, name)	
		logging.debug("Initialised monitor object for %s",self.name)

	def on_message(self, client, userdata, message):
		logging.debug("Received message on our target topic")
		logging.debug(message.payload)
		if self.current_state==self.return_state:
			if message.payload!=self.return_state:
				logging.debug("We've switched state")
				self.current_state=message
		else:
			if message.payload==self.return_state:
				logging.debug("We've gone back to the original state - notify!")
				self.current_state=message.payload
				self.method.notify(self.name+"::"+self.notify_message)
class presence_monitor(monitor):
	def __init__(self,topic, notify_message, method, name):
		self.times=[]
		self.last_timestamp=0
		self.training_complete=False
		self.average_time=0
		super(presence_monitor, self).__init__(topic, notify_message,method,name)
		logging.debug("Initialised monitor object for %s",self.name)

	def on_message(self, client, userdata, message):
		logging.debug("Received message on our target topic %s",message.topic)
		#record times for first five messages. We then use the average to alarm if we don't see any more
		if self.training_complete==False:
			self.times.append(time.time())	
			if len(self.times)==5:
				logging.debug("We've had five records, computing avg time between records")
				self.training_complete=True
				sum_time_diff=0
				previous_time=self.times[0]
				count=1	
				for time_val in self.times[1:]:
					time_diff = time_val-previous_time
					previous_time = time_val
					sum_time_diff = sum_time_diff + time_diff	

				self.average_time = float(sum_time_diff) / 5
	
				#Start a timer running
				logging.debug("Starting a thread to check we're getting messages - period %f",5*self.average_time)
				t = Timer(5*self.average_time, self.check_alive)
				t.start()
		else:
			#Log the timestamp, so the timer function can check we're still on track
			self.last_timestamp = time.time()


	def check_alive(self):
		if self.last_timestamp > (time.time() - (5*self.average_time)):
			#We have received a message in the time frame - all good
			logging.debug("We received a message %f, which is in the time frame %f", self.last_timestamp,time.time() - (5*self.average_time))
		else:
			self.method.notify(self.name+"::"+self.notify_message)
			logging.debug("Last message received %f, which is out ofthe time frame %f", self.last_timestamp,time.time() - (5*self.average_time))
		t = Timer(5*self.average_time, self.check_alive)
		t.start()

			
	
		

class slack:
	def __init__(self,token, channel, username):
		self.token = token
		self.channel=channel
		self.username=username
	def notify(self, text):
		sc = SlackClient(self.token)
		logging.debug("Sending message to slack: channel: %s, text: %s,username:%s",self.channel,text,self.username)
		sc.api_call(
		  "chat.postMessage",
		  channel=self.channel,
		  text=text,
		  username=self.username
		)


# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
    logging.debug("Connected with result code "+str(rc))

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
   
    client.subscribe("myhome/#")
 
    for monitor in monitors:
	client.message_callback_add(monitor.topic, monitor.on_message)
	logging.debug("Added callback for %s",monitor.topic)
    client.on_message = on_message

# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
	#logging.debug("Not handling message %s, on topic %s", msg.payload, msg.topic)
	pass
	#logging.debug("Received message on topic %s, payload %s",msg.topic, msg.payload)



parser = argparse.ArgumentParser(description='Temperature control for a room in the house.')
parser.add_argument("-c", "--config", dest='config_file',required=True, type=str, help="Config file (required)")
args = parser.parse_args()

Config = configparser.ConfigParser(defaults={'log_level': 'WARNING'})
if len(Config.read(args.config_file))==0:
	logging.error("Unable to read configuration file")
	exit(1)

#Set logging level
numeric_level = getattr(logging, Config.get('app','log_level').upper(), None)
if not isinstance(numeric_level, int):
    raise ValueError('Invalid log level: %s' % Config.get('app','log_level').upper())
fmt="%(levelname)s\t %(funcName)s():%(lineno)i: \t%(message)s"
logging.basicConfig(level=numeric_level,format=fmt)



client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

required_sections=['mqtt']
monitors = []
for section in required_sections:
	if section not in Config.sections():	
		logging.error("We need some config for the MQTT topic")
		exit(1)

for section in Config.sections():
	if section=='app':
		#Application-wide settings
		app_config=Config.get("app","log_level")
	elif section=='slack':
		#Configure slack output
		slack_config={}
		for thing in Config.options(section):
			slack_config[thing]=Config.get("slack",thing)
		slack = slack(slack_config['slack_token'],slack_config['channel'],slack_config['username'])
	elif section=='mqtt':
		mqtt_config={}
		for thing in Config.options(section):
			mqtt_config[thing]=Config.get("mqtt",thing)
	else:
		#instantiate new monitoring object based on settings
		if Config.get(section,"method")=='slack':
			if Config.get(section, "type")=='state':
				monitors.append(state_monitor(Config.get(section, "topic"),Config.get(section,"state"),Config.get(section,"message"),slack,section))
			elif Config.get(section, "type")=='presence':
				monitors.append(presence_monitor(Config.get(section, "topic"),Config.get(section,"message"),slack,section))
		else:
			logging.error("Unimplemented error - we can't handle this!")	

logging.debug("Connecting to broker %s:%p",mqtt_config['host'],mqtt_config['port'])
client.connect(mqtt_config['host'], mqtt_config['port'], 60)




exit

#Threaded loop for handling MQTT messages
client.loop_start()

while True:
	#We can use this loop for general housekeeping (e.g. checking that we have received confirmation messages)
	print("starting loop")
	time.sleep(1000)
