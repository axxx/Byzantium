# -*- coding: utf-8 -*-
# vim: set expandtab tabstop=4 shiftwidth=4 :

# networkconfiguration.py - Implements the network interface configuration
#    subsystem of the Byzantium control panel.

# Project Byzantium: http://wiki.hacdc.org/index.php/Byzantium
# License: GPLv3

# TODO:
# - Figure out what columns in the network configuration database to index.
#   It's doubtful that a Byzantium node would have more than three interfaces
#   (not counting lo) but it's wise to plan for the future.  Profile based on
#   which columns are SELECTed from most often.
# - Find a way to prune network interfaces that have vanished.
#   MOOF MOOF MOOF - Stubbed in.

import logging
import os
import os.path
import random
import re
import sqlite3
import subprocess
import time

import _utils
        

# Utility method to enumerate all of the network interfaces on a node.
# Returns two lists, one of wired interfaces and one of wireless
# interfaces.
def enumerate_network_interfaces():
    logging.debug("Entered NetworkConfiguration.enumerate_network_interfaces().")
    logging.debug("Reading contents of /sys/class/net/.")
    wired = []
    wireless = []

    # Enumerate network interfaces.
    interfaces = os.listdir('/sys/class/net')

    # Remove the loopback interface because that's our failure case.
    if 'lo' in interfaces:
        interfaces.remove('lo')

    # Failure case: If the list of interfaces is empty, return lists
    # containing only the loopback.
    if not interfaces:
        logging.debug("No interfaces found.  Defaulting.")
        return(['lo'], ['lo'])

    # For each network interface's pseudofile in /sys, test to see if a
    # subdirectory 'wireless/' exists.  Use this to sort the list of
    # interfaces into wired and wireless.
    for i in interfaces:
        logging.debug("Adding network interface %s.", i)
        if os.path.isdir("/sys/class/net/%s/wireless" % i):
            wireless.append(i)
        else:
            wired.append(i)
    return (wired, wireless)


# Method that generates an /etc/hosts.mesh file for the node for dnsmasq.
# Takes three args, the first and last IP address of the netblock.  Returns
# nothing.
def make_hosts(hosts_file, test, starting_ip=None):
    logging.debug("Entered NetworkConfiguration.make_hosts().")

    # See if the /etc/hosts.mesh backup file exists.  If it does, delete it.
    old_hosts_file = hosts_file + '.bak'
    if test:
        logging.debug("Deleted old /etc/hosts.mesh.bak.")
    else:
        if os.path.exists(old_hosts_file):
            os.remove(old_hosts_file)

    # Back up the old hosts.mesh file.
    if test:
        logging.debug("Renamed /etc/hosts.mesh file to /etc/hosts.mesh.bak.")
    else:
        if os.path.exists(hosts_file):
            os.rename(hosts_file, old_hosts_file)

    # We can make a few assumptions given only the starting IP address of
    # the client IP block.  Each node has a /24 netblock for clients, so
    # we only have to generate 254 entries for that file (.2-254).  First,
    # split the last octet off of the IP address passed to this method.
    (octet_one, octet_two, octet_three, _) = starting_ip.split('.')
    prefix = octet_one + '.' + octet_two + '.' + octet_three + '.'

    # Generate the contents of the new hosts.mesh file.
    if test:
        logging.debug("Pretended to generate new /etc/hosts.mesh file.")
        return False
    else:
        hosts = open(hosts_file, "w")
        line = prefix + str('1') + '\tbyzantium.byzantium.mesh\n'
        hosts.write(line)
        for i in range(2, 255):
            line = prefix + str(i) + '\tclient-' + prefix + str(i) + '.byzantium.mesh\n'
            hosts.write(line)
        hosts.close()

    # Test for successful generation of the file.
    error = False
    if not os.path.exists(hosts_file):
        os.rename(old_hosts_file, hosts_file)
        error = True
    return error

# Generates an /etc/dnsmasq.conf.include file for the node.  Takes one arg,
# the IP address to start from.
def configure_dnsmasq(dnsmasq_include_file, test, starting_ip=None):
    logging.debug("Entered NetworkConfiguration.configure_dnsmasq().")

    # Split the last octet off of the IP address passed into this
    # method.
    (octet_one, octet_two, octet_three, _) = starting_ip.split('.')
    prefix = octet_one + '.' + octet_two + '.' + octet_three + '.'
    start = prefix + str('2')
    end = prefix + str('254')

    # Use that to generate the line for the config file.
    # dhcp-range=<starting IP>,<ending IP>,<length of lease>
    dhcp_range = 'dhcp-range=' + start + ',' + end + ',5m\n'

    # If an include file already exists, move it out of the way.
    oldfile = dnsmasq_include_file + '.bak'
    if test:
        logging.debug("Deleting old /etc/dnsmasq.conf.include.bak file.")
    else:
        if os.path.exists(oldfile):
            os.remove(oldfile)

    # Back up the old dnsmasq.conf.include file.
    if test:
        logging.debug("Backing up /etc/dnsmasq.conf.include file.")
        logging.debug("Now returning to save time.")
        return
    else:
        if os.path.exists(dnsmasq_include_file):
            os.rename(dnsmasq_include_file, oldfile)

    # Open the include file so it can be written to.
    include_file = open(dnsmasq_include_file, 'w')

    # Write the DHCP range for this node's clients.
    include_file.write(dhcp_range)

    # Close the include file.
    include_file.close()

    # Restart dnsmasq.
    subprocess.Popen(['/etc/rc.d/rc.dnsmasq', 'restart'])
    return


# Constants.
# Ugly, I know, but we need a list of wi-fi channels to frequencies for the
# sanity checking code.
frequencies = [2.412, 2.417, 2.422, 2.427, 2.432, 2.437, 2.442, 2.447, 2.452,
               2.457, 2.462, 2.467, 2.472, 2.484]

# Classes.
# This class allows the user to configure the network interfaces of their node.
# Note that this does not configure mesh functionality.
class NetworkConfiguration(object):

    def __init__(self, templatelookup, test):
        self.templatelookup = templatelookup
        self.test = test

        # Location of the network.sqlite database, which holds the configuration
        # of every network interface in the node.
        if self.test:
            # self.netconfdb = '/home/drwho/network.sqlite'
            self.netconfdb = 'var/db/controlpanel/network.sqlite'
            logging.debug("Location of NetworkConfiguration.netconfdb: %s", self.netconfdb)
        else:
            self.netconfdb = '/var/db/controlpanel/network.sqlite'

        # Class attributes which make up a network interface.  By default they are
        # blank, but will be populated from the network.sqlite database if the
        # user picks an already-configured interface.
        self.mesh_interface = ''
        self.mesh_ip = ''
        self.client_interface = ''
        self.client_ip = ''
        self.channel = ''
        self.essid = ''
        self.bssid = '02:CA:FF:EE:BA:BE'
        self.ethernet_interface = ''
        self.ethernet_ip = ''
        self.frequency = 0.0
        self.gateway = 'no'

        # Set the netmasks aside so everything doesn't run together.
        self.mesh_netmask = '255.255.0.0'
        self.client_netmask = '255.255.255.0'

        # Attributes for flat files that this object maintains for the client side
        # of the network subsystem.
        self.hosts_file = '/etc/hosts.mesh'
        self.dnsmasq_include_file = '/etc/dnsmasq.conf.include'

    # Pretends to be index.html.
    def index(self):
        logging.debug("Entering NetworkConfiguration.index().")

        # Reinitialize this class' attributes in case the user wants to
        # reconfigure an interface.  It'll be used to set the default values
        # of the HTML fields.
        self.reinitialize_attributes()

        # Get a list of all network interfaces on the node (sans loopback).
        wired, wireless = enumerate_network_interfaces()
        logging.debug("Contents of wired[]: %s", wired)
        logging.debug("Contents of wireless[]: %s", wireless)

        # MOOF MOOF MOOF - call to stub implementation.  We can use the list
        # immediately above (interfaces) as the list to compare the database
        # against.
        # Test to see if any network interfaces have gone away.
        #logging.debug("Pruning missing network interfaces.")
        #self.prune(interfaces)

        # Build tables containing the interfaces extant.  At the same time,
        # search the network configuration databases for interfaces that are
        # already configured and give them a different color.  If they're up
        # and running give them yet another color.
        connection = sqlite3.connect(self.netconfdb)
        cursor = connection.cursor()
        wireless_buttons = ""
        ethernet_buttons = ""

        interface_tag_start = "<input type='submit' name='interface' value='"

        # Start with wireless interfaces.
        for i in wireless:
            logging.debug("Checking to see if %s is in the database.", i)
            cursor.execute("SELECT mesh_interface, enabled FROM wireless WHERE mesh_interface=?", (i, ))
            result = cursor.fetchall()

            # If the interface is not found in database, add it.
            if not result:
                logging.debug("Adding %s to table 'wireless'.", i)

                # gateway, client_interface, enabled, channel, essid,
                # mesh_interface
                template = ('no', (i + ':1'), 'no', '0', '', i, )

                cursor.execute("INSERT INTO wireless VALUES (?,?,?,?,?,?);", template)
                connection.commit()
                wireless_buttons += "%s%s' />\n" % (interface_tag_start, i)
                continue

            # If it is there test to see if it's been configured or not.  If it
            # has, use a CSS hack to make its button a different color.
            if result[0][1] == "yes":
                wireless_buttons += "%s%s' style='background-color:red' />\n" % (interface_tag_start, i)
                continue

            # If all else fails, just add the button without any extra
            # decoration.
            wireless_buttons += "%s%s' />\n" % (interface_tag_start, i)

        # Wired interfaces.
        for i in wired:
            logging.debug("Checking to see if %s is in the database.", i)
            cursor.execute("SELECT interface, enabled FROM wired WHERE interface=?", (i, ))
            result = cursor.fetchall()

            # If the interface is not found in database, add it.
            if not result:
                logging.debug("Adding %s to table 'wired'.", i)

                # enabled, gateway, interface
                template = ('no', 'no', i, )
                cursor.execute("INSERT INTO wired VALUES (?,?,?);", template)
                connection.commit()
                ethernet_buttons += "%s%s' />\n" % (interface_tag_start, i)
                continue

            # If it is found test to see if it's been configured or not.  If it
            # has, use a CSS hack to make its button a different color.
            if result[0][1] == "yes":
                ethernet_buttons += "%s%s' style='background-color:red' />\n" % (interface_tag_start, i)
                continue

            # If all else fails, just add the button without any extra
            # decoration.
            ethernet_buttons += "%s%s' />\n" % (interface_tag_start, i)

        # Render the HTML page.
        cursor.close()
        try:
            page = self.templatelookup.get_template("/network/index.html")
            return page.render(title = "Byzantium Node Network Interfaces",
                               purpose_of_page = "Configure Network Interfaces",
                               wireless_buttons = wireless_buttons,
                               ethernet_buttons = ethernet_buttons)
        except:
            _utils.output_error_data()
    index.exposed = True

    # Used to reset this class' attributes to a known state.
    def reinitialize_attributes(self):
        logging.debug("Reinitializing class attributes of NetworkConfiguration().")
        self.mesh_interface = ''
        self.client_interface = ''
        self.channel = ''
        self.essid = ''
        self.mesh_ip = ''
        self.client_ip = ''
        self.frequency = 0.0
        self.gateway = 'no'

    # This method is run every time the NetworkConfiguration() object is
    # instantiated by the admin browsing to /network.  It traverses the list
    # of network interfaces extant on the system and compares it against the
    # network configuration database.  Anything in the database that isn't in
    # the kernel is deleted.  Takes one argument, the list of interfaces the
    # kernel believes are present.
    # def prune(self, interfaces=None):
    #    logging.debug("Entered NetworkConfiguration.prune()")

    # Allows the user to enter the ESSID and wireless channel of their node.
    # Takes as an argument the value of the 'interface' variable defined in
    # the form on /network/index.html.
    def wireless(self, interface=None):
        logging.debug("Entered NetworkConfiguration.wireless().")

        # Store the name of the network interface chosen by the user in the
        # object's attribute set and then generate the name of the client
        # interface.
        self.mesh_interface = interface
        self.client_interface = interface + ':1'

        # Default settings for /network/wireless.html page.
        channel = 3
        essid = 'Byzantium'

        # This is a hidden class attribute setting, used for sanity checking
        # later in the configuration process.
        self.frequency = frequencies[channel - 1]

        channel, essid, warning = _utils.check_for_configured_interface(self.netconfdb, interface, channel, essid)

        # The forms in the HTML template do everything here, as well.  This
        # method only accepts input for use later.
        try:
            page = self.templatelookup.get_template("/network/wireless.html")
            return page.render(title = "Configure wireless for Byzantium node.",
                           purpose_of_page = "Set wireless network parameters.",
                           warning = warning, interface = self.mesh_interface,
                           channel = channel, essid = essid)
        except:
            _utils.output_error_data()
    wireless.exposed = True

    def get_raw_interface(self, interface):
        return interface.rsplit(":",1)[0]

    def get_unused_ip(self, interface, addr, kind):
        """docstring for get_unused_ip"""
        ip_in_use = 1
        interface = self.get_raw_interface(interface)
        while ip_in_use:
            # Run arping to see if any node in range has claimed that IP address
            # and capture the return code.
            # Argument breakdown:
            # -c 5: Send 5 packets
            # -D: Detect specified address.  Return 1 if found, 0 if not,
            # -f: Stop after the first positive response.
            # -I Network interface to use.  Mandatory.
            arping = ['/sbin/arping', '-c 5', '-D', '-f', '-q', '-I',
                      interface, addr]
            if self.test:
                logging.debug("NetworkConfiguration.tcpip() command to probe for a %s interface IP address is %s", kind, ' '.join(arping))
                time.sleep(5)
            else:
                ip_in_use = subprocess.call(arping)

            # arping returns 1 if the IP is in use, 0 if it's not.
            if not ip_in_use:
                logging.debug("IP address of %s interface is %s.", kind, addr)
                return addr
                
            # In test mode, don't let this turn into an endless loop.
            if self.test:
                logging.debug("Breaking out of this loop to exercise the rest of the code.")
                break

    def update_mesh_interface_status(self, status):
        """docstring for update_mesh_interface_status"""
        logging.debug("Setting wireless interface status: %s", status)
        command = ['/sbin/ifconfig', self.mesh_interface, status]
        if self.test:
            logging.debug("NetworkConfiguration.tcpip() command to update mesh interface status: %s", command)
        else:
            subprocess.Popen(command)

    # Implements step two of the interface configuration process: selecting
    # IP address blocks for the mesh and client interfaces.  Draws upon class
    # attributes where they exist but pseudorandomly chooses values where it
    # needs to.
    def tcpip(self, essid=None, channel=None):
        logging.debug("Entered NetworkConfiguration.tcpip().")

        # Store the ESSID and wireless channel in the class' attribute set if
        # they were passed as args.
        if essid:
            self.essid = essid
        if channel:
            self.channel = channel

        # Initialize the Python environment's randomizer.
        random.seed()

        # Connect to the network configuration database.
        connection = sqlite3.connect(self.netconfdb)
        cursor = connection.cursor()

        # To run arping, the interface has to be up.  Check the database to
        # see if it's up, and if not flip it on for a few seconds to test.
        template = (self.mesh_interface, 'yes', )
        cursor.execute("SELECT mesh_interface, enabled FROM wireless WHERE mesh_interface=? AND enabled=?;", template)
        result = cursor.fetchall()
        if not result:
            self.update_mesh_interface_status('up')

            # Sleep five seconds to give the hardware a chance to catch up.
            time.sleep(5)

        # First pick an IP address for the mesh interface on the node.
        # Go into a loop in which pseudorandom IP addresses are chosen and
        # tested to see if they have been taken already or not.  Loop until we
        # have a winner.
        logging.debug("Probing for an IP address for the mesh interface.")
        # Pick a random IP address in a 192.168/24.
        addr = '192.168.'
        addr = addr + str(random.randint(0, 254)) + '.'
        addr = addr + str(random.randint(1, 254))
        self.mesh_ip = self.get_unused_ip(self.mesh_interface, addr, kind="mesh")
    
        # Next pick a distinct IP address for the client interface and its
        # netblock.  This is potentially trickier depending on how large the
        # mesh gets.
        logging.debug("Probing for an IP address for the client interface.")
        # Pick a random IP address in a 10/24.
        addr = '10.'
        addr = addr + str(random.randint(0, 254)) + '.'
        addr = addr + str(random.randint(0, 254)) + '.1'
        self.mesh_ip = self.get_unused_ip(self.client_interface, addr, kind="client")

        # For testing, hardcode some IP addresses so the rest of the code has
        # something to work with.
        if self.test:
            self.mesh_ip = '192.168.1.1'
            self.client_ip = '10.0.0.1'

        # Deactivate the interface as if it was down to begin with.
        if not result:
            self.update_mesh_interface_status('down')

        # Close the database connection.
        connection.close()

        # Run the "Are you sure?" page through the template interpeter.
        try:
            page = self.templatelookup.get_template("/network/confirm.html")
            return page.render(title = "Confirm network address for interface.",
                               purpose_of_page = "Confirm IP configuration.",
                               interface = self.mesh_interface,
                               mesh_ip = self.mesh_ip,
                               mesh_netmask = self.mesh_netmask,
                               client_ip = self.client_ip,
                               client_netmask = self.client_netmask)
        except:
            _utils.output_error_data()
    tcpip.exposed = True

    # Configure the network interface.
    def set_ip(self):
        logging.debug("Entered NetworkConfiguration.set_ip().")

        # Set up the error catcher variable.
        error = []

        # Define the PID of the captive portal daemon in the topmost context
        # of this method.
        portal_pid = 0

        # If we've made it this far, the user's decided to (re)configure a
        # network interface.  Full steam ahead, damn the torpedoes!
        # First, take the wireless NIC offline so its mode can be changed.
        self.update_mesh_interface_status('down')
        time.sleep(5)

        # Wrap this whole process in a loop to ensure that stubborn wireless
        # interfaces are configured reliably.  The wireless NIC has to make it
        # all the way through one iteration of the loop without errors before
        # we can go on.
        while True:
            logging.debug("At top of wireless configuration loop.")

            chunks = {"mode": ("mode", "ad-hoc"),
                      "ESSID": ("essid", self.essid),
                      "BSSID": ("ap", self.bssid),
                      "channel": ("channel", self.channel)}
            for k, v in chunks.iteritems():
                logging.debug("Configuring wireless interface: %s = %s", k, v)
                command = ['/sbin/iwconfig', self.mesh_interface]
                command.extend(v)
                if self.test:
                    logging.debug("NetworkConfiguration.set_ip() command to set the %s: %s", k, ' '.join(command))
                else:
                    subprocess.Popen(command)
                    time.sleep(1)

            # Run iwconfig again and capture the current wireless configuration.
            command = ['/sbin/iwconfig', self.mesh_interface]
            configuration = ''
            if self.test:
                logging.debug("NetworkConfiguration.set_ip()command to capture the current state of a network interface: %s", command)
            else:
                output = subprocess.Popen(command, stdout=subprocess.PIPE).stdout
                configuration = output.readlines()

            break_flag = False
            # Test the interface by going through the captured text to see if
            # it's in ad-hoc mode.  If it's not, go back to the top of the
            # loop to try again.
            for line in configuration:
                if re.search("Mode|ESSID|Cell|Frequency", line):
                    line = line.split(' ')
                else:
                    continue

                if 'Mode' in line:
                    mode = line[0].split(':')[1]
                    if mode != 'Ad-Hoc':
                        logging.debug("Uh-oh!  Not in ad-hoc mode!  Starting over.")
                        break_flag = True
                        break

                # Test the ESSID to see if it's been set properly.
                if 'ESSID' in line:
                    essid = line[-1].split(':')[1]
                    if essid != self.essid:
                        logging.debug("Uh-oh!  ESSID wasn't set!  Starting over.")
                        break_flag = True
                        break

                # Test the BSSID to see if it's been set properly.
                if 'Cell' in line:
                    bssid = line[-1]
                    if bssid != self.bssid:
                        logging.debug("Uh-oh!  BSSID wasn't set!  Starting over.")
                        break_flag = True
                        break

                # Check the wireless channel to see if it's been set properly.
                if 'Frequency' in line:
                    frequency = line[2].split(':')[1]
                    if frequency != self.frequency:
                        logging.debug("Uh-oh!  Wireless channel wasn't set!  starting over.")
                        break_flag = True
                        break

            logging.debug("Hit bottom of the wireless configuration loop.")

            # For the purpose of testing, exit after one iteration so we don't
            # get stuck in an infinite loop.
            if self.test:
                break

            # "Victory is mine!"
            #     --Stewie, _Family Guy_
            if not(break_flag):
                break

        logging.debug("Wireless interface configured successfully.")

        # Call ifconfig and set up the network configuration information.
        logging.debug("Setting IP configuration information on wireless interface.")
        command = ['/sbin/ifconfig', self.mesh_interface, self.mesh_ip,
                   'netmask', self.mesh_netmask, 'up']
        if self.test:
            logging.debug("NetworkConfiguration.set_ip()command to set the IP configuration of the mesh interface: %s", command)
        else:
            subprocess.Popen(command)
        time.sleep(5)

        # Add the client interface.
        logging.debug("Adding client interface.")
        command = ['/sbin/ifconfig', self.client_interface, self.client_ip, 'up']
        if self.test:
            logging.debug("NetworkConfiguration.set_ip()command to set the IP configuration of the client interface: %s", command)
        else:
            subprocess.Popen(command)

        template = ('yes', self.channel, self.essid, self.mesh_interface, self.client_interface, self.mesh_interface)
        _utils.set_wireless_db_entry(self.netconfdb, template)

        # Start the captive portal daemon.  This will also initialize the IP
        # tables ruleset for the client interface.
        logging.debug("Starting captive portal daemon.")
        captive_portal_daemon = ['/usr/local/sbin/captive_portal.py', '-i',
                                 str(self.mesh_interface), '-a', self.client_ip,
                                 '-d' ]
        captive_portal_return = 0
        if self.test:
            logging.debug("NetworkConfiguration.set_ip() command to start the captive portal daemon: %s", captive_portal_daemon)
            captive_portal_return = 6
        else:
            captive_portal_return = subprocess.Popen(captive_portal_daemon)
        logging.debug("Sleeping for 5 seconds to see if a race condition is the reason we can't get the PID of the captive portal daemon.")
        time.sleep(5)

        # Now do some error checking.
        warnings = "<p>WARNING!  captive_portal.py exited with code %d - %s!</p>\n"
        if captive_portal_return == 1:
            error.append(warnings % (captive_portal_return, "insufficient command line arguments passed to daemon"))
        elif captive_portal_return == 2:
            error.append(warnings % (captive_portal_return, "bad arguments passed to daemon"))
        elif captive_portal_return == 3:
            error.append(warnings % (captive_portal_return, "bad IP tables commands during firewall initialization"))
        elif captive_portal_return == 4:
            error.append(warnings % (captive_portal_return, "bad parameters passed to IP tables"))
        elif captive_portal_return == 5:
            error.append(warnings % (captive_portal_return, "daemon already running on interface"))
        elif captive_portal_return == 6:
            error.append("<p>NOTICE: captive_portal.py started in TEST mode - did not actually start up!</p>\n")
        else:
            logging.debug("Getting PID of captive portal daemon.")

            # If the captive portal daemon started successfully, get its PID.
            # Note that we have to take into account both regular and test mode.
            captive_portal_pidfile = 'captive_portal.' + self.mesh_interface

            if os.path.exists('/var/run/' + captive_portal_pidfile):
                captive_portal_pidfile = '/var/run/' + captive_portal_pidfile
            elif os.path.exists('/tmp/' + captive_portal_pidfile):
                captive_portal_pidfile = '/tmp/' + captive_portal_pidfile
            else:
                error.append("<p>WARNING: Unable to open captive portal PID file " + captive_portal_pidfile + "</p>\n")
                logging.debug("Unable to find PID file %s of captive portal daemon.", captive_portal_pidfile)

            # Try to open the PID file.
            logging.debug("Trying to open %s.", captive_portal_pidfile)
            portal_pid = 0
            try:
                pidfile = open(str(captive_portal_pidfile), 'r')
                portal_pid = pidfile.readline()
                pidfile.close()
            except:
                error.append("<p>WARNING: Unable to open captive portal PID file " + captive_portal_pidfile + "</p>\n")

            logging.debug("value of portal_pid is %s.", portal_pid)
            if self.test:
                logging.debug("Faking PID of captive_portal.py.")
                portal_pid = "Insert clever PID for captive_portal.py here."

            if not portal_pid:
                portal_pid = "ERROR: captive_portal.py failed, returned code " + str(captive_portal_return) + "."
                logging.debug("Captive portal daemon failed to start.  Exited with code %s.", str(captive_portal_return))

        # Send this information to the methods that write the /etc/hosts and
        # dnsmasq config files.
        logging.debug("Generating dnsmasq configuration files.")

        problem = make_hosts(self.hosts_file, self.test, starting_ip=self.client_ip)
        if problem:
            error.append("<p>WARNING!  /etc/hosts.mesh not generated!  Something went wrong!</p>")
            logging.debug("Couldn't generate /etc/hosts.mesh!")
        configure_dnsmasq(self.dnsmasq_include_file, self.test, starting_ip=self.client_ip)

        # Render and display the page.
        try:
            page = self.templatelookup.get_template("/network/done.html")
            return page.render(title = "Network interface configured.",
                               purpose_of_page = "Configured!",
                               error = ''.join(error), interface = self.mesh_interface,
                               ip_address = self.mesh_ip,
                               netmask = self.mesh_netmask,
                               portal_pid = portal_pid,
                               client_ip = self.client_ip,
                               client_netmask = self.client_netmask)
        except:
            _utils.output_error_data()
    set_ip.exposed = True

