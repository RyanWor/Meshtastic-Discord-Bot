import asyncio
import json
import logging
import queue
import sys
import time
from datetime import datetime
import discord
import meshtastic
import meshtastic.serial_interface
import pytz
from discord import ButtonStyle, app_commands
from discord.ui import Button, View
from pubsub import pub

def load_config():
    try:
        with open("config.json", "r") as config_file:
            return json.load(config_file)
    except FileNotFoundError:
        logging.critical("The config.json file was not found.")
        raise
    except json.JSONDecodeError:
        logging.critical("config.json is not a valid JSON file.")
        raise
    except Exception as e:
        logging.critical(f"An unexpected error occurred while loading config.json: {e}")
        raise

config = load_config()

color = 0x67ea94 # Meshtastic Green

token = config["discord_bot_token"]
channel_id = int(config["discord_channel_id"])

channel_names = {
    0: "CHANNEL0",
    1: "CHANNEL1",
    2: "CHANNEL2",
    3: "CHANNEL3",
    4: "CHANNEL4",
    5: "CHANNEL5",
    6: "CHANNEL6",
    7: "CHANNEL7",
}

meshtodiscord = queue.Queue()
discordtomesh = queue.Queue()
nodelistq = queue.Queue()

def onConnectionMesh(interface, topic=pub.AUTO_TOPIC): # Called when reconnecting to the Meshtastic radio
    print(interface.myInfo)

def get_long_name(node_id, nodes):
    if node_id in nodes:
        return nodes[node_id]['user'].get('longName', 'Unknown')
    return 'Unknown'

def onReceiveMesh(packet, interface): # Called when a packet arrives from mesh
    try: 
        if packet['decoded']['portnum'] == 'TEXT_MESSAGE_APP':
            print("Text message packet received")  # For debugging
            print(f"Packet: {packet}")  # Print the entire packet for debugging

            # Check if 'channel' is present in the top-level packet
            if 'channel' in packet:
                channel_index = packet['channel']
            else:
                # Check if 'channel' is present in the decoded packet
                if 'channel' in packet['decoded']:
                    channel_index = packet['decoded']['channel']
                else:
                    channel_index = 0  # Default to channel 0 if not present
                    print("Channel not found in packet, defaulting to channel 0")  # For debugging

            channel_name = channel_names.get(channel_index, f"Unknown Channel ({channel_index})")

            current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

            nodes = interface.nodes
            from_long_name = get_long_name(packet['fromId'], nodes)
            to_long_name = get_long_name(packet['toId'], nodes) if packet['toId'] != '^all' else 'All Nodes'

            embed = discord.Embed(title="Message Received", description=packet['decoded']['text'], color=0x67ea94)
            embed.add_field(name="From Node", value=f"{from_long_name} ({packet['fromId']})", inline=True)
            embed.set_footer(text=f"{current_time}")

            if packet['toId'] == '^all':
                embed.add_field(name="To Channel", value=channel_name, inline=True)
            else:
                embed.add_field(name="To Node", value=f"{to_long_name} ({packet['toId']})", inline=True)

            meshtodiscord.put(embed)

    except KeyError as e: # Catch empty packet
        pass
    except Exception as e:  # Catch any other exceptions
        print(f"Unexpected error: {e}")  # For debugging
        pass

class MeshBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)
        self.iface = None  # Initialize iface as None

    async def setup_hook(self) -> None: # Create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.background_task())
        await self.tree.sync()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')

    async def background_task(self):
        await self.wait_until_ready()
        counter = 0
        nodelist = []
        channel = self.get_channel(channel_id)
        pub.subscribe(onReceiveMesh, "meshtastic.receive")
        pub.subscribe(onConnectionMesh, "meshtastic.connection.established")
        try:
            self.iface = meshtastic.serial_interface.SerialInterface()
        except Exception as ex:
            print(f"Error: Could not connect {ex}")
            sys.exit(1)
        while not self.is_closed():
            counter += 1
            if (counter % 12 == 1): # Approx 1 minute (every 12th call, call every 5 seconds), refresh node list
                nodelist = ["**Nodes seen in the last 15 minutes:**\n"]
                nodes = self.iface.nodes
                for node in nodes:
                    try:
                        id = str(nodes[node]['user']['id'])
                        longname = str(nodes[node]['user']['longName'])
                        if "hopsAway" in nodes[node]:
                            hopsaway = str(nodes[node]['hopsAway'])
                        else:
                            hopsaway = "0"
                        if "snr" in nodes[node]:
                            snr = str(nodes[node]['snr'])
                        else:
                            snr = "?"
                        if "lastHeard" in nodes[node]:
                            ts = int(nodes[node]['lastHeard'])
                            time_zone = pytz.timezone('YOUR-TZ')
                            current_time = datetime.fromtimestamp(ts, tz=pytz.utc).astimezone(time_zone)
                            timestr = current_time.strftime('%d %B %Y %I:%M:%S %p')
                        else:
                            # 15 minute time for active nodes
                            ts = time.time() - (16 * 60)
                            timestr = "Unknown"
                        if ts > time.time() - (15 * 60):
                            nodelist.append(f"\n**ID:** {id} | **Long Name:** {longname} | **Hops:** {hopsaway} | **SNR:** {snr} | **Last Heard:** {timestr}")
                    except KeyError as e:
                        print(e)
                        pass

                # Split nodelist into chunks of 10 rows
                nodelist_chunks = ["".join(nodelist[i:i+10]) for i in range(0, len(nodelist), 10)]

            try:
                meshmessage = meshtodiscord.get_nowait()
                if isinstance(meshmessage, discord.Embed):
                    await channel.send(embed=meshmessage)
                else:
                    await channel.send(meshmessage)
                meshtodiscord.task_done()
            except queue.Empty:
                pass
            try:
                meshmessage = discordtomesh.get_nowait()
                if meshmessage.startswith('channel='):
                    channel_index = int(meshmessage[8:meshmessage.find(' ')])
                    message = meshmessage[meshmessage.find(' ') + 1:]
                    self.iface.sendText(message, channelIndex=channel_index)
                elif meshmessage.startswith('nodenum='):
                    nodenum = int(meshmessage[8:meshmessage.find(' ')])
                    self.iface.sendText(meshmessage[meshmessage.find(' ') + 1:], destinationId=nodenum)
                else:
                    self.iface.sendText(meshmessage)
                discordtomesh.task_done()
            except:
                pass
            try:
                nodelistq.get_nowait()
                # If there are any item on this queue it sends the nodelist
                for chunk in nodelist_chunks:
                    await channel.send(chunk)
                nodelistq.task_done()
            except queue.Empty:
                pass
            await asyncio.sleep(5)

class HelpView(View):

    def __init__(self):
        super().__init__(timeout=None)

        # Create buttons
        self.add_item(Button(label="Kavitate", style=ButtonStyle.link, url="https://github.com/Kavitate"))
        self.add_item(Button(label="Mehtastic", style=ButtonStyle.link, url="https://meshtastic.org"))
        self.add_item(Button(label="Meshmap", style=ButtonStyle.link, url="https://meshmap.net"))

client = MeshBot(intents=discord.Intents.default())

@client.tree.command(name="help", description="Shows the help message.")
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    help_text = ("**Command List**\n"
         "`/sendid` - Send a message to another node.\n"
         "`/sendnum` - Send a message to another node.\n"
         "`/active` - Shows all active nodes.\n"
         "`/channelname0` to send a message in channel 0.\n"
         "`/channelname1` to send a message in channel 1.\n"
         "`/channelname2` to send a message in channel 2.\n"
         "`/channelname3` to send a message in channel 3.\n"
         "`/channelname4` to send a message in channel 4.\n"
         "`/channelname5` to send a message in channel 5.\n"
         "`/channelname6` to send a message in channel 6.\n"
         "`/channelname7` to send a message in channel 7.\n"
         "`/help` - Shows this help message.\n\n")

    color = 0x67ea94

    embed = discord.Embed(title="Meshtastic Bot Help", description=help_text, color=color)
    embed.set_footer(text="Meshtastic Discord Bot by Kavitate")
    ascii_art_image_url = "https://i.imgur.com/qvo2NkW.jpeg"
    embed.set_image(url=ascii_art_image_url)

    view = HelpView()
    await interaction.followup.send(embed=embed, view=view)

@client.tree.command(name="sendid", description="Send a message to a specific node.")
async def sendid(interaction: discord.Interaction, nodeid: str, message: str):
    try:
        # Strip the leading '!' if present
        if nodeid.startswith('!'):
            nodeid = nodeid[1:]

        # Convert hexadecimal node ID to decimal
        nodenum = int(nodeid, 16)

        current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

        embed = discord.Embed(title="Sending Message", description=message, color=0x67ea94)
        embed.add_field(name="To Node:", value=f"!{nodeid}", inline=True)  # Add '!' in front of nodeid
        embed.set_footer(text=f"{current_time}")
        await interaction.response.send_message(embed=embed, ephemeral=False)
        discordtomesh.put(f"nodenum={nodenum} {message}")
    except ValueError:
        error_embed = discord.Embed(title="Error", description="Invalid hexadecimal node ID.", color=0x67ea94)
        await interaction.response.send_message(embed=error_embed, ephemeral=True)

@client.tree.command(name="sendnum", description="Send a message to a specific node.")
async def sendnum(interaction: discord.Interaction, nodenum: int, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message", description=message, color=0x67ea94)
    embed.add_field(name="To Node:", value=str(nodenum), inline=True)
    embed.set_footer(text=f"{current_time}")
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"nodenum={nodenum} {message}")

@client.tree.command(name="CHANNEL-NAME-0", description="Send a message in CHANNEL-NAME-0.")
async def channelname0(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-0:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=0 {username_message}")

@client.tree.command(name="CHANNEL-NAME-1", description="Send a message in CHANNEL-NAME-1.")
async def channelname1(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-1:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=1 {username_message}")

@client.tree.command(name="CHANNEL-NAME-2", description="Send a message in CHANNEL-NAME-2.")
async def channelname2(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-2:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=2 {username_message}")

@client.tree.command(name="CHANNEL-NAME-3", description="Send a message in CHANNEL-NAME-3.")
async def channelname3(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-3:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=3 {username_message}")

@client.tree.command(name="CHANNEL-NAME-4", description="Send a message in CHANNEL-NAME-4.")
async def channelname4(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-4:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=4 {username_message}")

@client.tree.command(name="CHANNEL-NAME-5", description="Send a message in CHANNEL-NAME-5.")
async def channelname5(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-5:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=5 {username_message}")

@client.tree.command(name="CHANNEL-NAME-6", description="Send a message in CHANNEL-NAME-6.")
async def channelname6(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-6:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=6 {username_message}")

@client.tree.command(name="CHANNEL-NAME-7", description="Send a message in CHANNEL-NAME-7.")
async def channelname7(interaction: discord.Interaction, message: str):

    current_time = datetime.now().strftime('%d %B %Y %I:%M:%S %p')

    embed = discord.Embed(title="Sending Message to CHANNEL-NAME-7:", description=message, color=0x67ea94)
    embed.set_footer(text=f"{current_time}")
    username_message = f"{str(interaction.user.name)}[D]: " + message
    await interaction.response.send_message(embed=embed)
    discordtomesh.put(f"channel=7 {username_message}")

@client.tree.command(name="active", description="Lists all active nodes.")
async def active(interaction: discord.Interaction):
    await interaction.response.defer()

    nodelistq.put(True)
    
    await asyncio.sleep(1)

    await interaction.delete_original_response()

def run_discord_bot():
    try:
        client.run(token)
    except Exception as e:
        logging.error(f"An error occurred while running the bot: {e}")
    finally:
        if client:
            asyncio.run(client.close())

if __name__ == "__main__":
    run_discord_bot()
