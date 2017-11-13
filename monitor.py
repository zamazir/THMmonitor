# -*- coding: utf-8 -*-

##############################################################################
# Imports
##############################################################################
# Native libraries
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import sys
import os
import logging
import time
import datetime
import ntpath
import struct
import functools

import numpy as np
import matplotlib as mpl
mpl.use('Qt4Agg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt4agg import (FigureCanvasQTAgg
                                                as FigureCanvas)
from matplotlib.backends.backend_qt4agg import (NavigationToolbar2QT
                                                as NavigationToolbar)
from matplotlib import cm
from PyQt4 import QtGui
from PyQt4 import QtCore
from PyQt4.QtCore import (SIGNAL, QRect, pyqtSlot, pyqtSignal)
from PyQt4.uic import loadUiType
from PyQt4.QtGui import (QStyle, QHeaderView, QStyleOptionButton, QPainter,
                         QColor, QSizePolicy)
try:
    import pygame
    pygameImported = True
except ImportError:
    pygameImported = False

# Project libraries
import ops.hkdata as hkdata
import mapping
from rabbitmq import RabbitMQClient

__version__ = '3.2.0'

logging.basicConfig(filename='monitor.log',
                    filemode='a',
                    format='[%(asctime)s %(module)s:%(lineno)s] '
                           '%(levelname)-8s %(message)s',
                    datefmt='%d/%m/%Y %I:%M:%S %p',
                    level=logging.DEBUG)
logging.info('\n\n++++++++++++++ Program started +++++++++++++++++\n\n')

logger = logging.getLogger(__name__)
hdlr = logging.FileHandler('rawdata.log')
formatter = logging.Formatter('[%(asctime)s %(module)s:%(lineno)s] '
                              '%(levelname)-8s %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.setLevel(logging.INFO)

beaconTime = 0
globalData = {}
rawtime = {}
rawdata = {}


##############################################################################
# Matplotlib parameters
##############################################################################
mpl.rcParams.update({'figure.autolayout': True})
mpl.rcParams['svg.fonttype'] = 'none'


##############################################################################
# Argument Parsing
##############################################################################
modes = ['simulation', 'tvac', 'em', 'fm']
args = sys.argv
fileMode = False
if len(args) >= 2:
    fileMode = os.path.isfile(args[1])
    if args[1] in modes or fileMode:
        mode = args[1]
        print("mode {} is a known mode or a file".format(mode))
        logging.info("Received mode {} as command line argument".format(mode))
        if mode == 'em':
            rabbitMQConfig = 'config/rabbitmq_em.json'
        elif mode == 'fm':
            rabbitMQConfig = 'config/rabbitmq_fm.json'
    else:
        print("Unknown mode {}. Possible modes are {}.".format(args[1], modes))
        exit()
else:
    print("TVAC mode")
    mode = 'tvac'
    logging.info("Received no mode as command line argument. " +
                 "Assuming tvac mode")



##############################################################################
# Load UI
##############################################################################
script_dir = os.path.dirname(os.path.realpath(__file__))
UIpath = os.path.join(script_dir, 'monitor.ui')
Ui_MainWindow, QMainWindow = loadUiType(UIpath)


##############################################################################
# Threads
##############################################################################
class DataFrequencyThread(QtCore.QThread):
    def __init__(self, gui, updatethread):
        super(DataFrequencyThread, self).__init__()
        self.gui = gui
        self.times = [datetime.datetime.now()]
        self.frequency = 0
        self.nextDownlink = datetime.datetime.now()
        self.connect(updatethread,
                     SIGNAL('refresh_feed(PyQt_PyObject)'),
                     self.update)


    def __del__(self):
        self.wait()


    def appendTime(self, threshold=1):
        """
        Appends current time to array of times at which beacons were received.
        If the current time is less than `threshold` seconds after the last
        time, the current time is ignored in order to not mess up the beacon
        periodicity calculation.

        Receives:
            None
        Returns:
            None
        """
        t = datetime.datetime.now()
        # Filter beacons that arrive shortly after another beacon so frequency
        # is not influenced
        if (t - self.times[-1]).total_seconds() < threshold:
            return
        self.times.append(t)
        self.times = self.times[-100:]


    def computePeriodicity(self):
        periods = []
        for ind in range(len(self.times)):
            try:
                periods.append((self.times[ind + 1] - self.times[ind])
                               .total_seconds())
            except IndexError:
                break

        self.periods = periods
        try:
            if periods[-1] > 3 * np.mean(periods):
                self.emit(SIGNAL('beacon_gap_event(PyQt_PyObject)'),
                          self.times[-2])
        except IndexError:
            pass
        if len(periods) < 1:
            return 0
        return np.mean(periods)


    def reset(self):
        self.times = []


    def update(self, data=None):
        self.appendTime()
        dt = self.computePeriodicity()
        if not np.isnan(dt):
            downlinkIn = datetime.timedelta(seconds=dt)
            self.nextDownlink = datetime.datetime.now() + downlinkIn
            lastDownlink = str(max(self.times)).split('.')[0]
            statusMsg = ('Last downlink was at ' +
                         '<span style="font-weight:bold">{}</span>'
                         .format(lastDownlink) +
                         ' | Downlink normally every: ' +
                         '<span style="font-weight:bold">{:.1f} s</span>'
                         .format(dt) +
                         ' - Expecting next beacon in ' +
                         '<span style="font-weight:bold">T-{:.1f}</span>'
                         .format(downlinkIn.total_seconds()))
            self.gui.statusFrequency.setText(statusMsg)


    def updateStatus(self, tminus):
        statusMsgFreq = self.gui.statusFrequency.text().split('- ')[0]
        tminus = tminus.total_seconds()
        if tminus < 0:
            statusMsg = (statusMsgFreq +
                         '- Expecting next beacon in ' +
                         '<span style="font-weight:bold">T+{:.1f} s</span>'
                         .format(-tminus))
        else:
            statusMsg = (statusMsgFreq +
                         '- Expecting next beacon in ' +
                         '<span style="font-weight:bold">T-{:.1f} s</span>'
                         .format(tminus))
        self.gui.statusFrequency.setText(statusMsg)


    def overdueStatus(self):
        statusMsg = self.gui.statusFrequency.text()
        statusMsg += " BEACON OVERDUE"
        self.gui.statusFrequency.setText(statusMsg)


    def removeOverdueStatus(self):
        statusMsg = self.gui.statusFrequency.text()
        statusMsg = statusMsg.split(" BEACON OVERDUE")[0]
        self.gui.statusFrequency.setText(statusMsg)


    def run(self):
        while True:
            if len(self.times) < 2:
                time.sleep(1)
                continue
            now = datetime.datetime.now()
            tminus = self.nextDownlink - now
            self.updateStatus(tminus)
            if tminus.days < 0:
                self.overdueStatus()
            else:
                self.removeOverdueStatus()
            time.sleep(1)



class AlarmThread(QtCore.QThread):
    flash = pyqtSignal('PyQt_PyObject', 'PyQt_PyObject')
    stop = pyqtSignal()
    global pygameImported

    def __init__(self):
        super(AlarmThread, self).__init__()
        self.axes = None
        self.alarm = False

    def __del__(self):
        self.wait()


    def playWarningSound(self):
        pygame.mixer.init()
        pygame.mixer.music.load('warning.wav')
        pygame.mixer.music.play()


    def stopAlarm(self):
        self.disableAlarm()
        self.stop.emit()

    def disableAlarm(self):
        self.alarm = False

    def startAlarm(self):
        self.alarm = True
        if self.level == 1:
            color = 'orange'
        if self.level == 2:
            color = 'red'

        while self.alarm:
            self.flash.emit(True, color)
            if self.level == 2 and pygameImported:
                self.playWarningSound()
            time.sleep(1)
            self.flash.emit(False, None)
            time.sleep(1)


    def run(self):
        self.startAlarm()


class ConsumerThread(QtCore.QThread):
    refresh_feed = pyqtSignal('PyQt_PyObject')

    def run(self):
        print("Waiting for beacons...")

        def callback(self, channel, method, properties, body):
            global beaconTime
            body = eval(body)
            if method.routing_key == 'CDH':
                beaconTime = body['Beacon Timestamp']
                beaconTime = datetime.datetime.strptime(beaconTime,
                                                        '%Y-%m-%dT%H:%M:%S')
            if type(beaconTime).__name__ != 'datetime':
                return

            if method.routing_key == 'THM':
                print("Received THM beacon")
                print(body)
                del body['Beacon Timestamp']
                del body['Beacon Version']
                del body['Source']
                del body['Source ID']
                data = []
                for sensor, value in body.items():
                    data.append([sensor, beaconTime, value])
                self.emit(SIGNAL('refresh_feed(PyQt_PyObject)'), data)

        rmq = RabbitMQClient(rabbitMQConfig)
        rmq.start(functools.partial(callback, self))


class updateThread(QtCore.QThread):
    """
    This thread is responsible for any data aquisition and processing
    """
    add = pyqtSignal('PyQt_PyObject', 'PyQt_PyObject', 'PyQt_PyObject')
    progress = pyqtSignal('PyQt_PyObject', 'PyQt_PyObject', 'PyQt_PyObject')
    track = pyqtSignal()
    done = pyqtSignal()
    duplicatesReady = pyqtSignal('PyQt_PyObject')


    def __init__(self):
        super(updateThread, self).__init__()
        self.add.connect(self.addData)


    def __del__(self):
        self.wait()


    def handleError(self):
        time.sleep(5)


    def processFileData(self, data, purge=False, oldData={}):
        """
        Organizes journald THM file contents into a dictionary with sensor
        names as keys and tuples of x and y data numpy arrays as values. Ushers
        signal to trigger plotting of the data.

        Receives:
            List    data        The new data as found in the journald file
                                organized into a list line-by-line
            boolean purge       Whether or not to discard existing plot data
            dict    oldData     Data that has previously been loaded
        Signals:
            plot_all            Triggers plotting of the processed data
        Returns:
            None
        """
        if purge:
            plotData = {}
        else:
            plotData = oldData

        self.track.emit()
        size = len(data)
        dups = []
        for i, line in enumerate(data):
            for sensorData in line:
                if i % 100 == 0:
                    self.progress.emit(size, i,
                                       'Processing data (line {} of {})'
                                       .format(i, size))

                sensor = sensorData[0]
                newx = sensorData[1]
                newy = sensorData[2]

                # When plotting from file, system state not relevant
                if sensor in ('THM System State', 'State', 'Status'):
                    continue

                x, y = plotData.get(sensor, (np.zeros(0), np.zeros(0)))

                inds, = np.where(x == newx)
                for ind in inds:
                    if y[ind] == newy:
                        pass
                    else:
                        dt = mpl.dates.num2date(x[ind])
                        date = dt.date()
                        time = dt.time()
                        print("Two different values for sensor {} at {} {} "
                              .format(sensor, date, time) +
                              "found:\nOld: {}, New: {}."
                              .format(y[ind], newy))
                        dups.append(newx)
                newx = np.array([newx])
                newy = np.array([newy])
                x = np.concatenate((x, newx))
                y = np.concatenate((y, newy))

                plotData[sensor] = (x, y)
        self.duplicatesReady.emit(dups)
        self.done.emit()

        # Sort by time
        for sensor in plotData:
            x, y = plotData[sensor]

            p = x.argsort()
            x = x[p]
            y = y[p]

            plotData[sensor] = (x, y)

        self.emit(SIGNAL('plot_all(PyQt_PyObject)'), plotData)


    @pyqtSlot('PyQt_PyObject', 'PyQt_PyObject', 'PyQt_PyObject')
    def addData(self, file, purge, oldData):
        """
        Adds data from `file` to existing data or overwrites existing data.
        Delegates reading and interpretation to `dataFromFile` and plotting to
        `processFileData`

        Receives:
            Python file object      file    File containing the data to be
                                            added
            Boolean                 purge   Whether or not to retain existing
                                            data
            dict                    oldData Existing data
        """
        data = self.dataFromFile(file)
        if purge:
            self.emit(SIGNAL('discard_data()'))

        self.processFileData(data, purge, oldData)


    def convert(self, fileContents):
        """ Largely adapted from OPS_housekeeping thm_processor """
        sensorInfo = hkdata.thm_bytes
        # Split fileContents after 84 bytes and check if last byte is a newline
        # If it isn't, discard line, search next newline and try to read the 84
        # bytes after that
        time = None
        start = 0
        end = 0
        data = []
        size = len(fileContents)
        self.track.emit()
        while True:
            if end >= size:
                break
            lineData = []
            skip = 0
            self.progress.emit(size, start,
                               'Reading binary data (line {} of {})'
                               .format(start, size))
            for info in sensorInfo:
                sensorName = info[0]
                fmt = info[1]
                interpFunc = info[2]

                end = start + struct.calcsize(fmt)
                rawin = fileContents[start:end]

                try:
                    rawout = struct.unpack(fmt, rawin)[0]
                except struct.error as e:
                    logging.error("Failed to unpack raw value for {}"
                                  .format(sensorName) +
                                  " (value: {}, expected size: {}).\n"
                                  .format(rawin, struct.calcsize(fmt)) +
                                  "The last completely successfully read" +
                                  " data set was at {} (satellite time)"
                                  .format(mpl.dates.num2date(time)))
                    continue

                logging.info('{:40s}: {}-{} {:10} {:10}'
                             .format(sensorName, start, end - 1,
                                     rawin.__repr__(), rawout))
                if sensorName == 'Line terminator':
                    if rawin != '\n':
                        logging.error('Expected line break at bytes {} to {}.'
                                      .format(start, end - 1) +
                                      ' Got {} instead.\n'.format(rawout) +
                                      'Discarding data since last line break' +
                                      ' and skipping to the next one.\n' +
                                      'Discarded data:\n{}'.format(lineData))
                        skip = fileContents[end:].find('\n')
                        start = end + skip + 1
                        lineData = []
                        break
                start = end

                # This will only work reliably if the timestamp is the first
                # value after a line terminator
                if sensorName == 'Timestamp':
                    time = mpl.dates.epoch2num(rawout)
                    continue
                if sensorName in ['Line terminator', 'Status']:
                    continue

                try:
                    time
                except NameError:
                    logging.critical("Data was discarded due to lack of a " +
                                     "timestamp. This might have led to " +
                                     "wrong timestamps for other data. Be " +
                                     "careful when interpreting this data.")
                if interpFunc is None:
                    lineData.append([sensorName, time, rawout])
                else:
                    lineData.append([sensorName, time, interpFunc(rawout)])
            data.append(lineData)
        self.done.emit()
        return data


    def dataFromFile(self, fileName):
        """
        Reads a journald log file.

        Receives:
            string      fileName    File to be read
        Returns:
            list        data        Interpreted temperature data as
                                    provided by `interpretTHM`
        """
        # This was adapted from OPS_housekeeping thm_processor
        with open(fileName, 'rb') as f:
            fileContents = f.read()
        data = self.convert(fileContents)
        # This was used to read plain text files
        #data = []
        #with open(file, 'r') as f:
        #    for line in f.readlines():
        #        line = line.split()
        #        data.append(self.interpretTHM(line, stringify=False))
        return data


    def run(self):
        """
        Standard thread loop function.

        If in live mode, this function receives stdin messages and ushers
        a signal to trigger plotting of the new data. If in file mode, it reads
        the specified file using `dataFromFile` and delegates the data to
        `processFileData` for plotting.

        Receives:
            None
        Returns:
            None
        """
        global mode
        global fileMode

        if fileMode:
            data = self.dataFromFile(mode)
            self.processFileData(data)
            return

        while True:
            for line in sys.stdin:
                if mode == 'simulation':
                    time.sleep(5)
                    line = line.split()
                    line = self.interpretTHM(line)

                else:
                    if line.startswith('['):
                        line = ' '.join(line.split(']')[:-1]) + ']'
                    else:
                        continue

                if len(line) == 0:
                    logging.warning("No beacon data received. " +
                                    "Retrying in 5 seconds...")
                    self.handleError()
                    continue
                try:
                    data = eval(line)
                except:
                    logging.error("Beacon data could not be converted to " +
                                  "python object. Was the data passed in " +
                                  "the correct format?")
                    self.handleError()
                    continue
                self.emit(SIGNAL('refresh_feed(PyQt_PyObject)'), data)



##############################################################################
# Dialogs
##############################################################################
class MplLinestyleDialog(QtGui.QDialog):
    def __init__(self, parent=None, plotFamilies=None, colormaps=None):
        """
        plotFamilies must be in the format
            plotFamilies = {
                'familyName1': {
                    plot1, plot2, plot3, ...},
                'familyName2': {
                    plot1, plot2, plot3, ...},
                ...}
        Where plotx is an object with the attributes
            - name
            - linestyle
            - linewidth
            - marker
            - color
            - colormap
        colormaps must be in the format
            colormaps = {
                'familyName1': 'colormap1',
                ...}
        """
        super(MplLinestyleDialog, self).__init__(parent)

        self.oldStyle = {}
        self.families = plotFamilies
        self.combos = []
        self.comboColormaps = {}
        self.colorPatches = {}
        self.newStyle = {}
        self.attributes = ('linestyle', 'linewidth', 'color',
                           'colormap', 'marker')

        mainLayout = QtGui.QVBoxLayout()
        scrollContainer = QtGui.QScrollArea()
        familyContainer = QtGui.QWidget()
        self.familyLayout = QtGui.QGridLayout()
        btnResetAll = QtGui.QPushButton('Revert changes')
        currentRow = 0
        familyNames = sorted(plotFamilies.keys())

        self.familyLayout.addWidget(QtGui.QLabel('Global'), currentRow, 0)
        self.addAttributeCombo('linestyle', (currentRow, 2), isGlobal=True)
        self.addAttributeCombo('linewidth', (currentRow, 3), isGlobal=True)
        self.addAttributeCombo('marker', (currentRow, 4), isGlobal=True)
        currentRow += 1

        for familyName in familyNames:
            plotFamily = plotFamilies[familyName]
            lblFamily = QtGui.QLabel(familyName)

            self.familyLayout.addWidget(lblFamily,
                                        currentRow, 0, 1, 2)
            self.familyLayout.addWidget(QtGui.QLabel('Color'),
                                        currentRow, 1)
            self.familyLayout.addWidget(QtGui.QLabel('Linestyle'),
                                        currentRow, 2, 1, 2)
            self.familyLayout.addWidget(QtGui.QLabel('Linewidth'),
                                        currentRow, 3, 1, 2)
            self.familyLayout.addWidget(QtGui.QLabel('Marker'),
                                        currentRow, 4, 1, 2)
            currentRow += 1

            self.addAttributeCombo('linestyle', (currentRow, 2),
                                   familyName, familywide=True)
            self.addAttributeCombo('linewidth', (currentRow, 3),
                                   familyName, familywide=True)
            self.addAttributeCombo('marker', (currentRow, 4),
                                   familyName, familywide=True)

            colormap = colormaps[familyName]
            self.addAttributeCombo('colormap', (currentRow, 1),
                                   familyName, colormap)
            currentRow += 1

            plotFamily = sorted(plotFamily, key=lambda x: x.name)
            for plot in plotFamily:
                self.addPlotName((currentRow, 0), plot.name)
                self.addColorPatch((currentRow, 1), plot.name, plot.color)
                self.addAttributeCombo('linestyle', (currentRow, 2),
                                       plot.name, plot.linestyle)
                self.addAttributeCombo('linewidth', (currentRow, 3),
                                       plot.name, plot.linewidth)
                self.addAttributeCombo('marker', (currentRow, 4),
                                       plot.name, plot.marker)
                currentRow += 1

            self.familyLayout.addWidget(QtGui.QWidget(), currentRow, 0)
            currentRow += 1

        btnResetAll.clicked.connect(self.reset)

        buttonBox = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Ok |
                                           QtGui.QDialogButtonBox.Cancel,
                                           QtCore.Qt.Horizontal)
        buttonBox.addButton(btnResetAll, QtGui.QDialogButtonBox.ActionRole)
        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

        familyContainer.setMinimumSize(QtCore.QSize(600, 700))
        familyContainer.setLayout(self.familyLayout)
        scrollContainer.setWidget(familyContainer)
        mainLayout.addWidget(scrollContainer)
        mainLayout.addWidget(buttonBox)
        self.setLayout(mainLayout)

        self.setWindowTitle('Advanced plot style management')


    def changeAttribute(self, combo):
        if combo.familywide:
            family = combo.plotName
            names = [p.name for p in self.families[family]]
        if combo.isGlobal:
            names = [p.name for family in self.families.values()
                     for p in family]
        else:
            pass

        newValue = combo.currentText()

        for _combo in self.combos:
            isRightFamily = _combo.plotName in names
            isRightAttribute = _combo.attribute == combo.attribute
            isController = _combo.familywide or _combo.isGlobal
            if combo.familywide:
                if isRightAttribute and isRightFamily and not isController:
                    ind = _combo.findText(newValue)
                    _combo.setCurrentIndex(ind)
            elif combo.isGlobal:
                if isRightAttribute and not isController:
                    ind = _combo.findText(newValue)
                    _combo.setCurrentIndex(ind)


    def _addCombo(self, attribute, pos, plotName, options, currentOption,
                  tooltips=None, familywide=False, isGlobal=False):
        layout = self.familyLayout

        combo = Combo()
        combo.attribute = attribute
        combo.plotName = plotName
        combo.setMinimumWidth(70)
        if tooltips is not None:
            for option, tooltip in zip(options, tooltips):
                combo.addItem(option, (plotName,))
        else:
            for option in options:
                combo.addItem(option, (plotName,))

        if currentOption is None:
            combo.insertItem(0, '', (plotName,))
            combo.setCurrentIndex(0)
        else:
            currentOptionIndex = combo.findText(currentOption)
            if currentOptionIndex == -1:
                combo.addItem(currentOption, (plotName,))
                currentOptionIndex = combo.count() - 1
            combo.setCurrentIndex(currentOptionIndex)

        layout.addWidget(combo, *pos)

        if familywide:
            combo.familywide = True
        if isGlobal:
            combo.isGlobal = True

        self.combos.append(combo)

        return combo


    def addPlotName(self, pos, plotName):
        lblName = QtGui.QLabel(plotName)
        self.familyLayout.addWidget(lblName, *pos)


    def style(self):
        for plotName, patch in self.colorPatches.iteritems():
            color = self._pyqt2mplColor(patch.color)
            if 'color' not in self.newStyle:
                self.newStyle['color'] = {plotName: color}
            else:
                self.newStyle['color'].update({plotName: color})

        for combo in self.combos:
            ind = combo.currentIndex()
            value = str(combo.itemText(ind))
            attribute = combo.attribute
            plotName = combo.plotName
            if combo.isGlobal or combo.familywide:
                continue

            if attribute not in self.newStyle:
                self.newStyle[attribute] = {plotName: value}
            else:
                self.newStyle[attribute].update({plotName: value})
        return self.newStyle


    @pyqtSlot()
    def changeColormap(self, familyName):
        family = self.families[familyName]
        combo = self.comboColormaps[familyName]
        plotsInFamily = sorted([plot.name for plot in family])
        plotNumber = len(plotsInFamily)

        colormapName = str(combo.itemText(combo.currentIndex()))
        cmap = getattr(cm, colormapName)
        colors = cmap(np.linspace(0, 1, plotNumber))
        colorPatches = [self.colorPatches[plotName]
                        for plotName in plotsInFamily]

        for patch, color in zip(colorPatches, colors):
            color = [c * 255 for c in color]
            patch.setColor(color, 'rgb')


    def resetColors(self):
        colors = self.oldStyle['color']
        colorPatches = self.colorPatches
        for plotName, color in colors.iteritems():
            color = [c * 255 for c in color]
            patch = colorPatches[plotName]
            patch.setColor(color, 'rgb')


    def addColorPatch(self, pos, plotName, color):
        patch = ColorPatch(color)
        patch.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.familyLayout.addWidget(patch, *pos)

        patch.clicked.connect(functools.partial(self.changeColor, patch,
                                                plotName))

        if 'color' not in self.oldStyle:
            self.oldStyle['color'] = {}
        self.oldStyle['color'][plotName] = color
        self.colorPatches[plotName] = patch


    def _pyqt2mplColor(self, pyqtColor):
        isHex = False
        try:
            isHex = pyqtColor.startswith('#')
        except AttributeError:
            pass

        if isHex:
            color = mpl.colors.colorConverter.to_rgb(pyqtColor)
            red, green, blue = color
        else:
            red = pyqtColor.red() / 255.
            blue = pyqtColor.blue() / 255.
            green = pyqtColor.green() / 255.
        return (red, green, blue)


    @pyqtSlot('PyQt_PyObject', 'PyQt_PyObject')
    def changeColor(self, patch, plotName):
        dialog = QtGui.QColorDialog()
        dialog.setWindowState(dialog.windowState() &
                              ~QtCore.Qt.WindowMinimized |
                              QtCore.Qt.WindowActive)
        dialog.activateWindow()
        color = dialog.getColor()
        if not QColor.isValid(color):
            logging.error('Picked color is not valid')
            return

        patch.setColor(color)


    def addAttributeCombo(self, attribute, pos, plotName=None,
                          currentStyle=None, familywide=False,
                          isGlobal=False, styles=None, tooltips=None):
        if styles is None:
            if attribute == 'linestyle':
                styles = ('None', '-', '--', '-.', ':')
                tooltips = ('No line', 'Solid', 'Dashed', 'Dashdot', 'Dotted')

            elif attribute == 'linewidth':
                styles = [str(el) for el in range(1, 30)]

            elif attribute == 'marker':
                styles = ('None', '.', ',', 'o', 'v', '^', '<', '>',
                          '8', 's', 'p', 'P', '*', 'h', 'H', '+', 'x',
                          'X', 'D', 'd', '|', '_')
                tooltips = ('No marker', 'Dot', 'Pixel', 'Circle',
                            'Triangle down', 'Triangle up', 'Triangle left',
                            'Triangle right', 'Octagon', 'Square', 'Pentagon',
                            'Plus', 'Star', 'Hexagon 1', 'Hexagon 2', 'Plus',
                            'X', 'X (filled)', 'Diamond', 'Thin diamond',
                            'Vertical line', 'Horizontal line')
            elif attribute == 'colormap':
                styles = sorted(['gist_rainbow', 'viridis', 'plasma',
                                 'inferno', 'magma', 'Greys', 'Purples',
                                 'Blues', 'Greens', 'Oranges', 'Reds',
                                 'YlOrBr', 'YlOrRd', 'OrRd', 'PuRd', 'RdPu',
                                 'BuPu', 'GnBu', 'PuBu', 'YlGnBu', 'PuBuGn',
                                 'BuGn', 'YlGn', 'binary', 'gist_yarg',
                                 'gist_gray', 'gray', 'bone', 'pink', 'spring',
                                 'summer', 'autumn', 'winter', 'cool',
                                 'Wistia', 'hot', 'afmhot', 'gist_heat',
                                 'copper', 'PiYG', 'PRGn', 'BrBG', 'PuOr',
                                 'RdGy', 'RdBu', 'RdYlBu', 'RdYlGn',
                                 'Spectral', 'coolwarm', 'bwr', 'seismic',
                                 'Pastel1', 'Pastel2', 'Paired', 'Accent',
                                 'Dark2', 'Set1', 'Set2', 'Set3', 'flag',
                                 'prism', 'ocean', 'gist_earth', 'terrain',
                                 'gist_stern', 'gnuplot', 'gnuplot2', 'CMRmap',
                                 'cubehelix', 'brg', 'hsv', 'rainbow', 'jet',
                                 'nipy_spectral', 'gist_ncar'])
            else:
                print("Available styles for {} must be specified"
                      .format(attribute))
                return

        combo = self._addCombo(attribute, pos, plotName, styles, currentStyle,
                               tooltips, familywide, isGlobal)

        if familywide or isGlobal:
            combo.currentIndexChanged.connect(functools.partial(
                                              self.changeAttribute, combo))

        if attribute == 'colormap':
            familyName = plotName
            combo.currentIndexChanged.connect(functools.partial(
                                              self.changeColormap, familyName))
            combo.setMinimumWidth(150)
            self.comboColormaps[familyName] = combo
        else:
            combo.setMinimumWidth(70)

        if attribute not in self.oldStyle:
            self.oldStyle[attribute] = {}
        self.oldStyle[attribute][plotName] = currentStyle

        return combo


    def reset(self, attribute=None):
        self.resetCombos(attribute)
        self.resetColors()


    def resetCombos(self, attribute=None):
        if attribute is None or not attribute:
            attributes = self.attributes
        else:
            attributes = [attribute]

        for combo in self.combos:
            attr = combo.attribute
            plotName = combo.plotName

            if attr in attributes:
                originalValue = self.oldStyle[attr][plotName]
                if originalValue is None:
                    originalValue = ''
                ind = combo.findText(originalValue)
                combo.setCurrentIndex(ind)

        for family, colormap in self.oldStyle['colormap'].iteritems():
            comboColormap = self.comboColormaps[family]
            comboColormap.setCurrentIndex(comboColormap.findText(colormap))


    @staticmethod
    def getStyles(parent=None, family=None, colormaps=None):
        if family is None or colormaps is None:
            print("Plot family and colormaps must be specified")
            return {}
        dialog = MplLinestyleDialog(parent, family, colormaps)
        result = dialog.exec_()
        style = dialog.style()
        return (style, result == QtGui.QDialog.Accepted)



class SteadyStateDialog(QtGui.QDialog):
    def __init__(self, parent=None):
        super(SteadyStateDialog, self).__init__(parent)

        layout = QtGui.QFormLayout()
        self.lblThreshold = QtGui.QLabel('Maximum change in temperature')
        self.lblTimeRange = QtGui.QLabel('In the last ... minutes')
        self.editThreshold = QtGui.QLineEdit()
        self.editTimeRange = QtGui.QLineEdit()
        try:
            self.editThreshold.setText(str(parent.steadyStateThreshold))
            self.editTimeRange.setText(str(parent.steadyStateTimeRange))
        except AttributeError:
            pass

        buttonBox = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Ok |
                                           QtGui.QDialogButtonBox.Cancel,
                                           QtCore.Qt.Horizontal)
        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

        layout.addRow(self.lblThreshold, self.editThreshold)
        layout.addRow(self.lblTimeRange, self.editTimeRange)
        layout.addRow(buttonBox)

        self.setLayout(layout)
        self.setWindowTitle('Change steady state definition')

    def definition(self):
        threshold = self.editThreshold.text()
        timerange = self.editTimeRange.text()
        return threshold, timerange

    @staticmethod
    def getDefinition(parent=None):
        dialog = SteadyStateDialog(parent)
        result = dialog.exec_()
        thresh, time = dialog.definition()
        return (thresh, time, result == QtGui.QDialog.Accepted)



class AxesLimitsDialog(QtGui.QDialog):
    def __init__(self, parent=None):
        super(AxesLimitsDialog, self).__init__(parent)

        layout = QtGui.QFormLayout()
        self.lblxminDate = QtGui.QLabel('From:')
        self.lblxmaxDate = QtGui.QLabel('To:')
        self.lblymin = QtGui.QLabel('ymin:')
        self.lblymax = QtGui.QLabel('ymax:')
        self.lblcb = QtGui.QLabel('Save limits')
        self.lblSaveLabel = QtGui.QLabel('Save under label:')

        self.editxminDate = QtGui.QDateTimeEdit()
        self.editxmaxDate = QtGui.QDateTimeEdit()
        self.editymin = QtGui.QLineEdit()
        self.editymax = QtGui.QLineEdit()
        self.cbSave = QtGui.QCheckBox()
        self.editSaveLabel = QtGui.QLineEdit()

        now = datetime.datetime.now()
        dt = datetime.timedelta(minutes=10)
        self.editxminDate.setDateTime(now - dt)
        self.editxmaxDate.setDateTime(now)

        buttonBox = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Ok |
                                           QtGui.QDialogButtonBox.Cancel,
                                           QtCore.Qt.Horizontal)
        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

        layout.addRow(self.lblxminDate, self.editxminDate)
        layout.addRow(self.lblxmaxDate, self.editxmaxDate)
        layout.addRow(self.lblymin, self.editymin)
        layout.addRow(self.lblymax, self.editymax)
        layout.addRow(self.lblcb, self.cbSave)
        layout.addRow(self.lblSaveLabel, self.editSaveLabel)
        layout.addRow(buttonBox)

        self.setLayout(layout)
        self.setWindowTitle('Set window limits')

    @staticmethod
    def validate(val):
        try:
            val = float(val)
        except:
            val = None
        return val


    def saveState(self):
        return self.cbSave.isChecked(), self.editSaveLabel.text()


    def limits(self):
        xminDate = self.editxminDate.dateTime().toPyDateTime()
        xmaxDate = self.editxmaxDate.dateTime().toPyDateTime()
        xmin = mpl.dates.date2num(xminDate)
        xmax = mpl.dates.date2num(xmaxDate)
        ymin = self.editymin.text()
        ymax = self.editymax.text()
        return xmin, xmax, ymin, ymax


    @staticmethod
    def getLimits(parent=None):
        dialog = AxesLimitsDialog(parent)
        result = dialog.exec_()
        xmin, xmax, ymin, ymax = [dialog.validate(val)
                                  for val in dialog.limits()]
        save, label = dialog.saveState()
        return ([xmin, xmax], [ymin, ymax], [save, label],
                result == QtGui.QDialog.Accepted)



class AverageDialog(QtGui.QDialog):
    def __init__(self, parent=None):
        super(AverageDialog, self).__init__(parent)

        layout = QtGui.QFormLayout()
        self.lblPoints = QtGui.QLabel('Number of points to average over')
        self.lblMethod = QtGui.QLabel('Numpy method used for averaging')
        self.editPoints = QtGui.QLineEdit()
        self.editMethod = QtGui.QLineEdit()
        try:
            self.editPoints.setText(str(parent._points))
            self.editMethod.setText(str(parent._averageMethod))
        except AttributeError:
            pass

        buttonBox = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Ok |
                                           QtGui.QDialogButtonBox.Cancel,
                                           QtCore.Qt.Horizontal)
        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

        layout.addRow(self.lblPoints, self.editPoints)
        layout.addRow(self.lblMethod, self.editMethod)
        layout.addRow(buttonBox)

        self.setLayout(layout)
        self.setWindowTitle('Data averaging')

    def definition(self):
        points = int(self.editPoints.text())
        method = str(self.editMethod.text())
        return points, method

    @staticmethod
    def getSettings(parent=None):
        dialog = AverageDialog(parent)
        result = dialog.exec_()
        points, method = dialog.definition()
        return (points, method, result == QtGui.QDialog.Accepted)



##############################################################################
# Core Classes
##############################################################################
class Window(QtCore.QObject):
    global mode
    global fileMode
    progress = pyqtSignal('PyQt_PyObject', 'PyQt_PyObject', 'PyQt_PyObject')
    track = pyqtSignal()
    doneSig = pyqtSignal()

    def __init__(self, gui):
        super(Window, self).__init__()
        self.fig = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)

        self.canvas.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.canvas.setFocus()

        self.sensors = []
        self.spikes = []
        self.dups = []
        self.dupLines = []
        self.graphs = {}
        self.colors = {}
        self.data = {}
        self.compareAx = None
        self.compareLims = None
        self.marker = '+'

        self.ax.xaxis_date()
        self.updateTicks()

        self.ax.set_xlabel('Time')
        self.ax.set_ylabel('Sensor temperature [$^\circ$C]')

        self.updateThread = updateThread()
        self.freqThread = DataFrequencyThread(gui, self.updateThread)
        self.gui = gui
        self.fig.canvas.mpl_connect('motion_notify_event', self.onMove)
        self.fig.canvas.mpl_connect('button_release_event', self.onRelease)
        self.connect(self.gui.consumer,
                     SIGNAL('refresh_feed(PyQt_PyObject)'),
                     self.update)


    def updateAverage(self, points, method):
        """
        Averages the data points on the canvas over intervals with `points`
        points using the numpy method `method` by delegating the graph data to
        `update()`. If applied successively, the averaging effects will add to
        each other: Averaging over 5 and then over 2 points will effectively
        average the original data over 10 points. The original data can be
        restored using `restore()`.

        Receives:
            integer     `points`    Number of data points to average over
            string      `method`    Numpy method to use for averaging
        Returns:
            None
        """
        for sensor, graph in self.graphs.iteritems():
            x = graph.get_xdata()
            y = graph.get_ydata()

            try:
                x, y = self.average(x, y, points, method)
            except AttributeError as e:
                print(str(e))

            graph.set_xdata(x)
            graph.set_ydata(y)

        self.ax.draw_artist(self.ax.patch)
        for graph in self.graphs.values():
            self.ax.draw_artist(graph)

        self.ax.grid(True)
        self.canvas.update()
        self.canvas.flush_events()


    def restore(self):
        """
        Restores the original data points. This method can be used to undo
        averaging after using `averagePlot`.

        Receives:
            None
        Returns:
            None
        """
        for sensor, sensorData in self.data.iteritems():
            x, y = sensorData

            graph = self.graphs[sensor]
            graph.set_xdata(x)
            graph.set_ydata(y)

        self.ax.draw_artist(self.ax.patch)
        for graph in self.graphs.values():
            self.ax.draw_artist(graph)

        self.ax.grid(True)
        self.canvas.update()
        self.canvas.flush_events()


    def average(self, x, y, points, method='mean'):
        """
        Bins `x` and `y` into bins comprising `points` points.

        Receives:
        args
            numpy array `x`         Array to be binned
            numpy array `y`         Array to be binned
            integer     `points`    Number of point over which to average
        kwargs
            string      `method`    Numpy method to use for averaging.
                                    Default: 'mean'

        Returns:
            numpy array `avgx`      Binned array
            numpy array `avgy`      Binned array
        """
        bins = x.size / points
        rest = x.size % points
        avgx = np.zeros(bins + 1)
        avgy = np.zeros(bins + 1)

        method = getattr(np, method)

        j = 0
        for i in range(bins):
            avgx[i] = method(x[j:j + points])
            avgy[i] = method(y[j:j + points])
            j += points

        avgx[-1] = method(x[-rest:])
        avgy[-1] = method(y[-rest:])

        return avgx, avgy


    def getTVACambientTemperatures(self):
        file = QtGui.QFileDialog.getOpenFileName(self.gui,
                                                 'Choose TVAC temperature ' +
                                                 'source file')

        ok = os.path.isfile(file)
        if ok:
            with open(file, 'r') as f:
                lines = f.readlines()

            lines = lines[1:]
            size = len(lines)
            sets = np.zeros(size)
            temps = np.zeros(size)
            datetimes = np.zeros(size)
            setsFiltered = []
            datetimesFiltered = []

            self.track.emit()
            print("Getting TVAC temperatures")
            for i, line in enumerate(lines):
                if i % 5 == 0:
                    self.progress.emit(size, i,
                                       "Getting TVAC temperatures " +
                                       "(line {} of {})".format(i, size))

                cols = [el.replace(',', '.') for el in line.split()]
                date = cols[0]
                time = cols[1].split('.')[0]
                tempSet = cols[2]
                tempIs = cols[3]

                datefmt = '%d.%m.%Y %H:%M:%S'
                dtime = datetime.datetime.strptime(date + ' ' + time, datefmt)
                dtime = mpl.dates.date2num(dtime)

                sets[i] = tempSet
                temps[i] = tempIs
                datetimes[i] = dtime
            self.doneSig.emit()

            # Filter temperature setting data to only contain the first and
            # last point of a constant setting to save processor time when
            # plotting
            for i, val in enumerate(sets):
                if i + 1 == sets.size:
                    break
                if sets[i + 1] != sets[i]:
                    setsFiltered.append(sets[i])
                    setsFiltered.append(sets[i + 1])
                    datetimesFiltered.append(datetimes[i])
                    datetimesFiltered.append(datetimes[i + 1])

            setsFiltered = np.array(setsFiltered)
            datetimesFiltered = np.array(datetimesFiltered)

            return (datetimes, temps), (datetimesFiltered, setsFiltered)


    def zoomOut(self, ax):
        """
        This function ensures that all plots in the Axes object `ax` are
        visible and zooms to the y-values in the shown range. Finds
        corresponding axes limits. Sets object attribute `compareLims` to the
        found values and sets the ylims of `ax` to `compareLims`. This function
        only considers Line2D objects (no scatter plots).

        Receives:
            Matplotlib Axes instance    ax      The axes to be zoomed to
                                                contents
        Returns:
            None
        """
        xmin, xmax = ax.xaxis.get_view_interval()
        data = []
        for line in ax.lines:
            isSetting = str(line.get_label()) == 'setting'
            if line.get_visible() and not isSetting:
                xdata = line.get_xdata()
                ydata = line.get_ydata()
                filtered = ydata[np.where(xmin <= xdata & xdata <= xmax)]
                data.append(list(filtered))

        data = [float(item) for sublist in data for item in sublist]
        if len(data) == 0:
            return
        miny, maxy = [min(data), max(data)]

        if miny > 0:
            miny *= 0.95
        else:
            miny *= 1.05
        if maxy > 0:
            maxy *= 1.05
        else:
            maxy *= 0.95

        self.compareLims = (miny, maxy)

        ax.set_ylim(self.compareLims, emit=False)


    def setCompareAxis(self, quantity):
        """
        Creates second Axes object that has a common x-axis with the main Axes.
        Draws the quantity `quantity` in these Axes.

        Receives:
            string      quantity    The quantity to be drawn in the second Axes
                                    object
        Returns:
            None
        """
        quantity = str(quantity)
        if self.compareAx is not None:
            self.compareAx.remove()
            self.compareAx = None
            self.canvas.draw()
        if quantity in (None, 'none', 'None'):
            return

        self.compareAx = self.ax.twinx()
        self.compareAx.callbacks.connect('ylim_changed', self.zoomOut)

        if quantity == 'TVAC temperature':
            result = self.getTVACambientTemperatures()
            if result is None:
                return
            (times, temps), (setTimes, settings) = result
            self.compareAx.plot(times, temps, color='black')
            self.compareAx.plot(setTimes, settings, color='black', ls='--',
                                label='setting')

            self.compareAx.set_ylabel('TVAC temperature [$^\circ$C]')
            self.zoomOut(self.compareAx)
        else:
            print("Didn't recognize quantity")
        self.canvas.draw()


    def applyTimeZone(self, tzDelta):
        """
        Shifts all plotted lines in x-direction by `tzDelta` hours.

        Receives:
            integer     tzDelta     Number of hours by which all plots should
                                    be shifted in x-direction
        Returns:
            None
        """
        for line in self.ax.lines:
            times = line.get_xdata()
            delta = datetime.timedelta(hours=tzDelta)
            times = [mpl.dates.date2num(mpl.dates.num2date(time) + delta)
                     for time in times]
            line.set_xdata(times)
        self.canvas.draw()


    def onMove(self, event):
        if event.inaxes:
            x = event.xdata
            y = event.ydata
            prettyDate = str(mpl.dates.num2date(x)).split('.')[0]
            self.gui.statusbar.showMessage(u'x = {}, y = {:.1f}\u00b0C'
                                           .format(prettyDate, y))
        else:
            self.gui.statusbar.clearMessage()


    def onRelease(self, event):
        if self.gui.toolbar._active == 'ZOOM':
            self.gui.toolbar.release_zoom(event)
        elif self.gui.toolbar._active == 'PAN':
            self.gui.toolbar.release_pan(event)
        self.updateTicks()
        # This is necessary so ticks will be refreshed after releasing
        # mouse -.-
        p, = self.ax.plot(range(10))
        p.remove()
        self.fig.canvas.draw()
        p, = self.ax.plot(range(10))
        p.remove()
        self.fig.canvas.draw()


    def toggleSensors(self, selectedSensors):
        for sensor in self.sensors:
            selected = sensor.name in selectedSensors
            plotted = sensor.name in self.graphs
            if plotted:
                graph = self.graphs[sensor.name]
            else:
                print('Sensor without graph encountered: {}'
                      .format(sensor.name))
                logging.error(
                    "Graph for sensor {} seems to not have been initialized."
                    .format(sensor.name))

            visible = graph.get_visible()
            if selected and not visible:
                graph.set_visible(True)
            if not selected and visible:
                graph.set_visible(False)

        self.canvas.draw()


    def str2mpldate(self, str):
        if fileMode or mode == 'simulation':
            # str should already be a mpl date in this case
            return str

        if type(str).__name__ != 'datetime':
            time = datetime.datetime.strptime(str, '%Y-%m-%dT%H:%M:%S')
        else:
            time = str

        # Convert time zones (CET --> local)
        time = time + datetime.timedelta(hours=self.gui.timeZone)
        return mpl.dates.date2num(time)


    def initGraphs(self, data):
        logging.info("Initializing graphs")
        self.graphs = {}

        for sensorData in data:
            sensor = sensorData[0]
            if sensor in ('THM System State', 'State', 'System State'):
                continue
            x = sensorData[1]
            y = sensorData[2]

            self.sensors.append(Sensor(sensor))

            try:
                y = float(y)
            except ValueError:
                y = np.nan
            except TypeError:
                print("{} cannot be converted to float.".format(y) +
                      "Argument must be a string or a number")
                y = np.nan

            timestamp = self.str2mpldate(x)
            self.graphs[sensor] = self.ax.plot_date([timestamp], [y])[0]

        self.sensors = sorted(self.sensors, key=lambda x: x.name)
        uniqueSubsystems = list(set([s.subsystem for s in self.sensors]))
        self.subsystems = sorted(uniqueSubsystems)

        self.assignColors()

        self.updateTicks()
        showGrid = self.gui.cbShowGrid.isChecked()
        if showGrid:
            self.ax.grid(True)
        self.canvas.draw()


    def assignColors(self):
        colorsBySubsystem = {}
        self.colormaps = {'ADCS': 'gist_rainbow',
                          'CDH': 'summer',
                          'COM': 'autumn',
                          'Payload': 'inferno',
                          'EPS': 'winter'}

        for sensor in self.sensors:
            sub = sensor.subsystem
            sensorsInSub = len([s for s in self.sensors if s.subsystem == sub])
            if sub in self.colormaps:
                colormap = getattr(cm, self.colormaps[sub])
            else:
                print("No colormap specified for subsystem {}. ".format(sub) +
                      "Using gist_rainbow.")
                colormap = getattr(cm, 'gist_rainbow')
            colorsBySubsystem[sub] = colormap(np.linspace(0, 1, sensorsInSub))

        self.colors = {}
        for sub in self.subsystems:
            colors = colorsBySubsystem[sub]
            subsystemSensors = [s for s in self.sensors if s.subsystem == sub]
            for sensor, color in zip(subsystemSensors, colors):
                self.colors[sensor.name] = color

        for sensor, graph in self.graphs.iteritems():
            graph.set_color(self.colors[sensor])


    def goLive(self):
        logging.info("Starting data retrieval")
        self.gui.disconnect(self.updateThread,
                            SIGNAL('refresh_feed(PyQt_PyObject)'),
                            self.update)
        self.gui.disconnect(self.updateThread,
                            SIGNAL('finished()'),
                            self.done)
        self.gui.disconnect(self.updateThread,
                            SIGNAL('plot_all(PyQt_PyObject)'),
                            self.plotAll)
        self.gui.disconnect(self.updateThread,
                            SIGNAL('discard_data()'),
                            self.clear)
        try:
            self.updateThread.duplicatesReady\
                .disconnect(self.gui.receiveDuplicates)
        except TypeError:
            pass

        self.gui.connect(self.updateThread,
                         SIGNAL('refresh_feed(PyQt_PyObject)'),
                         self.update)
        self.gui.connect(self.updateThread,
                         SIGNAL('finished()'),
                         self.done)
        self.gui.connect(self.updateThread,
                         SIGNAL('plot_all(PyQt_PyObject)'),
                         self.plotAll)
        self.gui.connect(self.updateThread,
                         SIGNAL('discard_data()'),
                         self.clear)
        self.updateThread.duplicatesReady.connect(self.gui.receiveDuplicates)

        self.updateThread.start()
        self.freqThread.start()


    def done(self):
        logging.info("Data retrieval stopped")


    def started(self):
        logging.info("Data retrieval started")


    def stahp(self):
        if self.updateThread.isFinished():
            return
        logging.info("Stopping data retrieval")
        self.updateThread.quit()
        self.updateThread.terminate()
        self.freqThread.quit()
        self.freqThread.terminate()
        self.gui.statusbar.showMessage('Feed stopped', 2000)


    def getSelectedSensors(self):
        if hasattr(self.gui, 'checkBoxes'):
            selectedSensors = [sensor for sensor, cb
                               in self.gui.checkBoxes.iteritems()
                               if cb.isChecked()]
        else:
            selectedSensors = [s.name for s in self.sensors]
        return selectedSensors


    def markDuplicates(self):
        for x in self.dups:
            self.dupLines.append(self.ax.axvline(x, 0, 1,
                                                 color='grey',
                                                 ls='--'))
        self.canvas.draw()


    def removeDuplicateMarkers(self):
        for marker in self.dupLines:
            marker.remove()
        self.dupLines = []
        self.canvas.draw()


    def plotAll(self, plotData):
        """ Plots all data from a file instantly """
        self.ax.clear()

        self.sensors = [Sensor(name) for name in sorted(plotData.keys())]
        uniqueSubsystems = list(set([s.subsystem for s in self.sensors]))
        self.subsystems = sorted(uniqueSubsystems)

        self.track.emit()
        size = len(plotData)
        for i, (sensor, data) in enumerate(plotData.iteritems()):
            self.progress.emit(size, i, "Plotting graph ({})".format(sensor))
            x, y = data
            graph = self.ax.plot_date(x, y,
                                      marker=self.marker,
                                      ls='solid')[0]
            self.graphs[sensor] = graph
        self.doneSig.emit()

        self.assignColors()

        if self.gui.sensorTable.rowCount() == 0:
            self.gui.populateSensorTable(self.sensors)

        if self.gui.cbShowGrid.isChecked():
            self.ax.grid(True)
        else:
            self.ax.grid(False)

        self.data = plotData
        self.gui.zoom('all')
        self.canvas.draw()


    def _appendBeaconData(self, data, newData):
        global rawtime
        global rawdata
        for sensorData in newData:
            sensor = str(sensorData[0])
            if sensor not in rawtime:
                rawtime[sensor] = []
                rawdata[sensor] = []
            rawtime[sensor].append(sensorData[1])
            rawdata[sensor].append(sensorData[2])

            if sensor == 'THM System State':
                continue
            time = self.str2mpldate(sensorData[1])
            value = float(sensorData[2])

            if sensor not in data:
                data[sensor] = [[], []]
            data[sensor][0].append(time)
            data[sensor][1].append(value)

            # Sort by time
            times = data[sensor][0]
            values = data[sensor][1]

            times, values = zip(*sorted(zip(times, values)))

            data[sensor][0] = list(times)
            data[sensor][1] = list(values)

        logger.info('{}'.format(datetime.datetime.now()))
        logger.info(rawtime)
        logger.info(rawdata)
        logger.info('\n\n')

        return data


    def update(self, data, live=True):
        """
        Append data and re-draw canvas. This method is only invoked in live
        mode.
        """
        global globalData

        # Display warning if warning state
        for sensorData in data:
            sensor = sensorData[0]
            if sensor in ('THM System State', 'State', 'System State'):
                newy = sensorData[2]
                if newy == 1:
                    logging.warning('SYSTEM IN WARNING STATE')
                    self.gui.warning(newy)
                elif newy == 2:
                    logging.warning('!!!!!! SYSTEM IN CRITICAL STATE !!!!!!')
                    self.gui.warning(newy)
                break

        if len(self.graphs) == 0:
            self.initGraphs(data)

        globalData = self._appendBeaconData(globalData, data)

        selectedSensors = self.getSelectedSensors()

        xtot = []
        ytot = []
        oldxLim = self.ax.get_xlim()
        oldyLim = self.ax.get_ylim()
        fixedView = not self.gui.cbAutoUpdate.isChecked()
        try:
            xLimit = float(self.gui.editWindowWidth.text())
        except:
            xLimit = None

        for sensor, sensorData in globalData.items():
            times, values = sensorData
            selected = sensor in selectedSensors
            graph = self.graphs[sensor]

            if live:
                self.checkSteadyState(sensor, times, values)

            # This might lead to problems if the program has been running for
            # a long time and xtot gets huge. Consider reducing it to unique
            # values
            xtot.extend(times)
            ytot.extend(values)

            logging.debug("Current data for {}:".format(sensor))
            logging.debug(list(zip(times, values)))

            # Show current temperature values
            # This has to happen after the arrays were chronologically sorted
            try:
                label = self.gui.tempLabels[sensor]
                label.setText('{:.1f}'.format(values[-1]) + u'\u00b0C')
                label.update()
            except:
                pass

            try:
                graph.remove()
            except:
                pass
            marker = self.marker
            if self.gui.cbShowLines.isChecked():
                graph = self.ax.plot_date(times, values,
                                          color=self.colors[sensor],
                                          marker=marker,
                                          ls='solid')[0]
            else:
                graph = self.ax.plot_date(times, values,
                                          color=self.colors[sensor],
                                          marker=None,
                                          ls='none')[0]

            if not selected:
                graph.set_visible(False)

            self.graphs[sensor] = graph

        # Adjust view
        if fixedView:
            self.ax.set_xlim(oldxLim)
            self.ax.set_ylim(oldyLim)
        else:
            if min(xtot) == max(xtot):
                date = mpl.dates.num2date(max(xtot))
                padLeft = datetime.timedelta(minutes=5)
                padRight = datetime.timedelta(minutes=1)
                self.ax.set_xlim(date - padLeft, date + padRight)
            else:
                maxDate = mpl.dates.num2date(max(xtot))
                xmin = mpl.dates.num2date(self.ax.get_xlim()[0])
                if xLimit:
                    range = maxDate - xmin
                    minutes = xLimit
                    xLimit = datetime.timedelta(minutes=minutes)
                    if range > xLimit:
                        maxDate += datetime.timedelta(minutes=minutes / 10)
                        self.ax.set_xlim([maxDate - xLimit, maxDate])
                    else:
                        try:
                            padRight = (maxDate - xmin) / 20
                        except TypeError:
                            padRight = datetime.timedelta(minutes=5)
                        self.ax.set_xlim(xmin, maxDate + padRight)
                else:
                    try:
                        padRight = (maxDate - xmin) / 20
                    except TypeError:
                        padRight = datetime.timedelta(minutes=5)
                    self.ax.set_xlim(xmin, maxDate + padRight)

        if self.gui.sensorTable.rowCount() == 0:
            self.gui.populateSensorTable(self.sensors)

        if live:
            self.updateTicks()
            self.canvas.draw()


    def getSensor(self, sensorName):
        sensorList = [s for s in self.sensors if s.name == sensorName]
        if len(sensorList) > 0:
            sensor = sensorList[0]
        else:
            print("No sensor {} found".format(sensorName))
            sensor = None
        return sensor


    def checkSteadyState(self, sensor, times, data):
        times = [mpl.dates.num2date(time) for time in times]
        threshold = self.gui.steadyStateThreshold
        dt = datetime.timedelta(minutes=self.gui.steadyStateTimeRange)
        inSteadyState = self.getSensor(sensor).steady
        y = []

        if sensor in ('THM System State', 'State', 'System State'):
            return
        for _time, _y in zip(times, data):
            if max(times) - dt < _time <= max(times):
                y.append(_y)

        try:
            if abs(min(y) - max(y)) < threshold:
                if not inSteadyState:
                    self.emit(SIGNAL(
                        'steady_state_changed(PyQt_PyObject, PyQt_PyObject)'),
                        sensor, True)
            else:
                if inSteadyState:
                    self.emit(SIGNAL(
                        'steady_state_changed(PyQt_PyObject, PyQt_PyObject)'),
                        sensor, False)
        except ValueError:
            pass


    def updateTicks(self, xmin=None, xmax=None):
        dateFmtMajor, dateFmtMinor = self.getDateFormat(xmin, xmax)
        self.ax.xaxis.set_major_formatter(dateFmtMajor)
        self.ax.xaxis.set_minor_formatter(dateFmtMinor)
        try:
            self.fig.autofmt_xdate()
        except RuntimeError:
            locator = mpl.dates.AutoDateLocator()
            self.ax.xaxis.set_minor_locator(locator)
            self.ax.xaxis.set_major_locator(locator)
            dateFmt = mpl.dates.AutoDateFormatter(locator)
            self.ax.xaxis.set_major_formatter(dateFmt)
            self.ax.xaxis.set_minor_formatter(dateFmt)
            self.fig.autofmt_xdate()


    def getDateFormat(self, xmin, xmax):
        if xmin is None:
            xmin = self.ax.get_xlim()[0]
        if xmax is None:
            xmax = self.ax.get_xlim()[1]
        dateRange = mpl.dates.num2date(xmax) - mpl.dates.num2date(xmin)

        def maxMinutes(x):
            return datetime.timedelta(minutes=x)

        def seconds(x):
            return mpl.dates.SecondLocator(interval=x)

        def minutes(x):
            return mpl.dates.MinuteLocator(interval=x)

        def hours(x):
            return mpl.dates.HourLocator(interval=x)

        def days(x):
            return mpl.dates.DayLocator(interval=x)

        def months(x):
            return mpl.dates.MonthLocator(interval=x)

        def years(x):
            return mpl.dates.YearLocator(x)

        if dateRange < maxMinutes(1):
            self.ax.xaxis.set_minor_locator(seconds(10))
            self.ax.xaxis.set_major_locator(seconds(30))
            dateFmtMajor = mpl.dates.DateFormatter('%M:%S')
            dateFmtMinor = mpl.dates.DateFormatter('%S')

        elif dateRange < maxMinutes(20):
            self.ax.xaxis.set_minor_locator(seconds(30))
            self.ax.xaxis.set_major_locator(minutes(2))
            dateFmtMajor = mpl.dates.DateFormatter('%H:%M:%S')
            dateFmtMinor = mpl.dates.DateFormatter('%M:%S')

        elif dateRange < maxMinutes(40):
            self.ax.xaxis.set_minor_locator(minutes(2))
            self.ax.xaxis.set_major_locator(minutes(5))
            dateFmtMajor = mpl.dates.DateFormatter('%H:%M')
            dateFmtMinor = mpl.dates.DateFormatter('%M:%S')

        elif dateRange < maxMinutes(120):
            self.ax.xaxis.set_minor_locator(minutes(10))
            self.ax.xaxis.set_major_locator(minutes(20))
            dateFmtMajor = mpl.dates.DateFormatter('%b %d %H:%M')
            dateFmtMinor = mpl.dates.DateFormatter('%M:%S')

        elif dateRange < maxMinutes(480):
            self.ax.xaxis.set_minor_locator(minutes(15))
            self.ax.xaxis.set_major_locator(minutes(30))
            dateFmtMajor = mpl.dates.DateFormatter('%b %d %H:%M')
            dateFmtMinor = mpl.dates.DateFormatter('%M:%S')

        elif dateRange < maxMinutes(12 * 60):
            self.ax.xaxis.set_minor_locator(minutes(30))
            self.ax.xaxis.set_major_locator(hours(1))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d %H:%M')
            dateFmtMinor = mpl.dates.DateFormatter('%Hh')

        elif dateRange < maxMinutes(24 * 60):
            self.ax.xaxis.set_minor_locator(hours(1))
            self.ax.xaxis.set_major_locator(hours(3))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d %Hh')
            dateFmtMinor = mpl.dates.DateFormatter('%Hh')

        elif dateRange < maxMinutes(24 * 60 * 3):
            self.ax.xaxis.set_minor_locator(hours(3))
            self.ax.xaxis.set_major_locator(hours(12))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d %Hh')
            dateFmtMinor = mpl.dates.DateFormatter('%d %Hh')

        elif dateRange < maxMinutes(24 * 60 * 10):
            self.ax.xaxis.set_minor_locator(hours(12))
            self.ax.xaxis.set_major_locator(days(1))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d')
            dateFmtMinor = mpl.dates.DateFormatter('%d %Hh')

        elif dateRange < maxMinutes(24 * 60 * 365 / 12):
            self.ax.xaxis.set_minor_locator(days(1))
            self.ax.xaxis.set_major_locator(days(5))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d')
            dateFmtMinor = mpl.dates.DateFormatter('%d')

        elif dateRange < maxMinutes(24 * 60 * 3 * 365 / 12):
            self.ax.xaxis.set_minor_locator(days(1))
            self.ax.xaxis.set_major_locator(days(10))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m-%d')
            dateFmtMinor = mpl.dates.DateFormatter('%d')

        elif dateRange < maxMinutes(24 * 60 * 365):
            self.ax.xaxis.set_minor_locator(days(10))
            self.ax.xaxis.set_major_locator(months(1))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m')
            dateFmtMinor = mpl.dates.DateFormatter('')

        elif dateRange < maxMinutes(24 * 60 * 365 * 3):
            self.ax.xaxis.set_minor_locator(months(1))
            self.ax.xaxis.set_major_locator(months(3))
            dateFmtMajor = mpl.dates.DateFormatter('%Y-%m')
            dateFmtMinor = mpl.dates.DateFormatter('')

        else:
            self.ax.xaxis.set_minor_locator(months(1))
            self.ax.xaxis.set_major_locator(years(1))
            dateFmtMajor = mpl.dates.DateFormatter('%Y')
            dateFmtMinor = mpl.dates.DateFormatter('')

        dateFmtMinor = mpl.dates.DateFormatter('')

        return dateFmtMajor, dateFmtMinor


    def clear(self):
        """
        Clears the window axes and resets all attributes that hold data related
        to the plots.

        Receives:
            None
        Returns:
            None
        """
        restart = False
        if self.updateThread.isRunning():
            self.stahp()
            restart = True

        self.ax.clear()
        self.canvas.draw()
        self.data = {}
        self.graphs = {}
        self.sensors = []
        self.colors = {}

        if restart:
            self.goLive()



class Monitor(QMainWindow, Ui_MainWindow):
    global fileMode

    def __init__(self):
        super(Monitor, self).__init__()
        self.setupUi(self)

        global __version__

        self.consumer = ConsumerThread()
        self.createWindow()

        self.toolbar = NavigationToolbar(self.window.canvas, self)
        self.toolbar.hide()
        self.toolbar.pan()

        self.steadyStateThreshold = 0.5
        self.steadyStateTimeRange = 60
        self.timeZone = 0
        self._points = 2
        self._averageMethod = 'mean'
        self.dupLines = []

        self.comboLimits.insertItem(0, 'Fit all data', ('showAll',))
        self.comboLimits.insertItem(1, 'Fit x data', ('showAllX',))
        self.comboLimits.insertItem(2, 'Fit y data', ('showAllY',))

        self.statusFrequency = QtGui.QLabel()
        self.statusTimeZone = QtGui.QLabel()
        self.statusSteadyState = QtGui.QLabel()
        self.statusFrequency.setMinimumWidth(600)
        if not fileMode:
            self.statusbar.addPermanentWidget(self.statusSteadyState)
            self.statusbar.addPermanentWidget(self.statusFrequency)
        self.statusbar.addPermanentWidget(self.statusTimeZone)
        self.updateTimeZoneText()
        if not fileMode:
            self.updateSteadyStateText()

        self.progressbar = QtGui.QProgressBar()
        self.progressbar.setMinimum(0)
        self.progressbar.setMaximum(100)
        self.statusbar.addPermanentWidget(self.progressbar)

        self.setWindowTitle('THM temperature monitor v{} - {}'
                            .format(__version__, mode))
        # Header
        columnNames = ['', 'Color', 'Live', 'Sensor']
        header = MyHeader(QtCore.Qt.Horizontal, columnNames, self)
        self.sensorTable.setHorizontalHeader(header)
        self.sensorTable.setColumnCount(len(columnNames))

        header = self.sensorTable.horizontalHeader()
        self.sensorTable.verticalHeader().setVisible(False)
        self.sensorTable.setColumnWidth(0, 20)
        self.sensorTable.setColumnWidth(1, 70)
        self.sensorTable.setColumnWidth(2, 100)
        self.sensorTable.setColumnWidth(3, 400)
        header.setDefaultAlignment(QtCore.Qt.AlignLeft)

        self.window.fig.set_facecolor('#999999')
        self.window.canvas.draw()

        # Make sensors aware of subsystem mapping
        Sensor.mapping = mapping.mapping

        self.alarmThread = AlarmThread()

        self.menuTestWarning.triggered.connect(functools.partial(
                                               self.warning, 1))
        self.menuTestCritical.triggered.connect(functools.partial(
                                                self.warning, 2))
        self.btnClear.clicked.connect(self.clearMessage)
        self.btnPan.clicked.connect(self.activatePan)
        self.btnZoom.clicked.connect(self.activateZoom)
        self.btnSave.clicked.connect(self.saveFigure)
        self.btnShowAll.clicked.connect(self.applyAxesLimits)
        self.cbShowLines.stateChanged.connect(self.refresh)
        self.cbShowMarkers.stateChanged.connect(self.refresh)
        self.cbShowGrid.stateChanged.connect(self.refresh)
        self.menuChangeTimeZone.triggered.connect(self.pickTimeZone)
        self.menuChangeAxesLimits.triggered.connect(self.changeAxesLimits)
        self.menuChangeMarker.triggered.connect(self.changeMarker)
        self.menuSaveCurrentLimits.triggered.connect(self.saveAxesLimits)
        self.menuMarkSpikes.triggered.connect(self.pickThreshold)
        self.menuRemoveSpikeMarkers.triggered.connect(self.removeSpikeMarkers)
        self.comboCompareTo.currentIndexChanged.connect(self.compareTo)
        self.alarmThread.flash.connect(self.showFlashScreen)
        self.alarmThread.stop.connect(self.stopAlarm)
        self.menuAdvancedLinestyleSettings\
            .triggered.connect(self.advancedLinestyleSettings)
        self.menuAbout_2.triggered.connect(self.about)
        link = ('https://redmine.move2space.de/projects/move2/' +
                'wiki/How_to_use_the_THM_monitor')
        self.menuOnlineDoc.triggered.connect(
            functools.partial(self.openLink, link))

        if not fileMode:
            self.menuChangeSteadyState.triggered.connect(
                self.changeSteadyStateDefinition)
            self.menuAddData.setEnabled(False)
            self.menuAverage.setEnabled(False)
            self.menuNewData.setEnabled(False)
            self.menuRestore.setEnabled(False)
            self.menuDuplicates.setEnabled(False)
        else:
            self.menuAddData.triggered.connect(self.addData)
            self.menuAverage.triggered.connect(self.averagePlot)
            self.menuNewData.triggered.connect(functools.partial(self.addData,
                                                                 True))
            self.menuRestore.triggered.connect(self.restorePlot)
            self.menuMarkDuplicates.triggered.connect(self.markDuplicates)
            self.menuRemoveDupMarkers.triggered.connect(
                self.removeDuplicateMarkers)
            self.menuChangeSteadyState.setEnabled(False)

        if not fileMode:
            signal = 'steady_state_changed(PyQt_PyObject, PyQt_PyObject)'
            self.connect(self.window, SIGNAL(signal), self.applySteadyState)
            signal = 'beacon_gap_event(PyQt_PyObject)'
            self.connect(self.window.freqThread, SIGNAL(signal),
                         self.logMissingBeacon)
        self.connect(self.window.updateThread,
                     SIGNAL('discard_data()'),
                     self.clearSensorTable)

        self.window.updateThread.progress.connect(self.trackProgress)
        self.window.updateThread.track.connect(self.startTrackingProgress)
        self.window.updateThread.done.connect(self.stopTrackingProgress)
        self.window.progress.connect(self.trackProgress)
        self.window.track.connect(self.startTrackingProgress)
        self.window.doneSig.connect(self.stopTrackingProgress)

        if mode in ('em', 'fm'):
            self.startConsuming()
        else:
            self.startFeed()


    def startConsuming(self):
        self.consumer.start()


    def openLink(self, link):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))


    def about(self):
        """ Shows information about the application in a message box. """
        global __version__
        QtGui.QMessageBox.about(self, "About",
                                "THM temperature monitor\n\n" +
                                "Stream live temperature data or load " +
                                "housekeeping temperature log files.\n\n"

                                "Version: {}\n".format(__version__) +
                                "Written by: Amazigh Zerzour\n" +
                                "E-mail: amazigh.zerzour@gmail.com ")

    @staticmethod
    def getExtrema(lines):
        y = []
        for line in lines:
            if line.get_visible():
                y.extend(line.get_ydata())
        if len(y) < 1:
            return [None, None]
        return [min(y), max(y)]


    def receiveDuplicates(self, dups):
        self.window.dups.extend(dups)
        self.window.dups = list(set(self.window.dups))


    def markDuplicates(self):
        self.window.markDuplicates()


    def removeDuplicateMarkers(self):
        self.window.removeDuplicateMarkers()


    def advancedLinestyleSettings(self):
        plotFamilies = {sub: [] for sub in self.window.subsystems}

        for name, graph in self.window.graphs.iteritems():
            sub, = [s.subsystem for s in self.window.sensors if s.name == name]
            colormap = self.window.colormaps[sub]
            plot = Plot(graph, name, colormap)
            plotFamilies[sub].append(plot)

        newStyles, ok = MplLinestyleDialog.getStyles(self,
                                                     plotFamilies,
                                                     self.window.colormaps)

        if ok:
            for attribute, plotStyles in newStyles.iteritems():
                for name, style in plotStyles.iteritems():
                    isColormap = attribute == 'colormap'
                    if name in self.window.subsystems and isColormap:
                        self.window.colormaps[name] = style
                        continue
                    if attribute == 'linewidth':
                        style = int(style)
                    if attribute == 'color':
                        patch = self.patches[name]
                        color = [c * 255 for c in style]
                        patch.setColor(color, 'rgb')
                    mpl.artist.setp(self.window.graphs[name],
                                    **{attribute: style})

            self.window.canvas.draw()


    def showWarningPopup(self):
        try:
            self.warnMsg.close()
        except:
            pass
        self.warnMsg = QtGui.QMessageBox()
        self.warnMsg.setIcon(QtGui.QMessageBox.Warning)
        self.warnMsg.setText('System state: ' +
                             '<span style="font-weight:bold">WARNING</span>')
        self.warnMsg.setInformativeText('Check subsystem temperatures and ' +
                                        'adjust operational mode and/or ' +
                                        'shroud temperature')
        self.warnMsg.setWindowTitle('System state warning')
        self.warnMsg.setStandardButtons(QtGui.QMessageBox.Ok)
        self.warnMsg.setStyleSheet("QLabel{min-width: 200px;}")
        self.warnMsg.buttonClicked.connect(self.stopAlarm)

        # Jump to front
        self.setWindowState(self.windowState() & ~QtCore.Qt.WindowMinimized |
                            QtCore.Qt.WindowActive)
        self.activateWindow()
        self.warnMsg.setWindowState(self.warnMsg.windowState() &
                                    ~QtCore.Qt.WindowMinimized |
                                    QtCore.Qt.WindowActive)
        self.warnMsg.activateWindow()

        self.warnMsg.exec_()


    def showCriticalPopup(self):
        try:
            self.critMsg.quit()
        except:
            pass
        try:
            self.warnMsg.quit()
        except:
            pass
        self.critMsg = QtGui.QMessageBox()
        self.critMsg.setIcon(QtGui.QMessageBox.Critical)
        self.critMsg.setText('System state: ' +
                             '<span style="font-weight:bold">CRITICAL</span>')
        self.critMsg.setInformativeText('Safe mode may have to be entered! ' +
                                        'Check sensor temperatures.')
        self.critMsg.setDetailedText('One or more of the sensors are ' +
                                     'reading temperature values exceeding ' +
                                     'the allowed range. Check if this is ' +
                                     'just a read-out error. If not, ' +
                                     'immediately enter safe mode')
        self.critMsg.setWindowTitle('System state warning')
        self.critMsg.setStandardButtons(QtGui.QMessageBox.Ok)
        self.critMsg.setStyleSheet("QLabel{min-width: 200px;}")
        self.critMsg.buttonClicked.connect(self.stopAlarm)

        # Jump to front
        self.setWindowState(self.windowState() & ~QtCore.Qt.WindowMinimized |
                            QtCore.Qt.WindowActive)
        self.activateWindow()
        self.critMsg.setWindowState(self.critMsg.windowState() &
                                    ~QtCore.Qt.WindowMinimized |
                                    QtCore.Qt.WindowActive)
        self.critMsg.activateWindow()

        self.critMsg.exec_()


    def startTrackingProgress(self):
        pass
        #print "Starting to track progress"


    def stopTrackingProgress(self):
        #print "Stopping progress tracking"
        self.progressbar.setValue(0)
        self.statusbar.clearMessage()


    def trackProgress(self, size, i, msg=None):
        prog = i * 100 / size

        def showProgressInTerminal():
            sys.stdout.write("\r[ {:20s} ] {}%".format('#' * (prog / 5), prog))
            if msg:
                sys.stdout.write(" ({})".format(msg))

            sys.stdout.flush()

        def updateProgressbar():
            self.progressbar.setValue(prog)
            self.statusbar.showMessage(msg)

        #showProgressInTerminal()
        updateProgressbar()


    def changeMarker(self):
        marker, ok = QtGui.QInputDialog.getText(self,
                                                'Change data point marker ' +
                                                'style',
                                                'Matplotlib marker style ' +
                                                '(e.g. *, ^, D, ...):')
        if ok:
            graphs = self.window.graphs.values()
            for graph in graphs:
                mpl.artist.setp(graph, marker=str(marker))
            self.window.marker = str(marker)
            self.window.canvas.draw()


    def addData(self, purge=False):
        """
        Opens file dialog to choose file to read data from. Delegates file to
        `updateThread` to retreive and plot data. Re-titles the window
        according to what files are currently loaded.

        Receives:
            boolean     purge       Whether or not to retain existing data
        Returns:
            None
        """
        file = QtGui.QFileDialog.getOpenFileName(self,
                                                 caption='Load file')
        file = str(file)
        if os.path.isfile(file):
            self.window.updateThread.add.emit(file, purge, self.window.data)

            title = str(self.windowTitle())
            hasFilenameInTitle = len(title.split('-')) > 1
            if hasFilenameInTitle:
                if purge:
                    title = title.split('-')[0] + ' - ' + ntpath.basename(file)
                else:
                    title = title + ' + ' + ntpath.basename(file)
            self.setWindowTitle(title)


    def discardData(self):
        """
        Clears window (see window.clear()) and sensor table.
        """
        self.window.clear()
        self.clearSensorTable()


    def clearSensorTable(self):
        self.sensorTable.setRowCount(0)


    def changeAxesLimits(self):
        xlim, ylim, saveInfo, ok = AxesLimitsDialog.getLimits()
        if ok:
            save, label = saveInfo
            xlimCurrent = self.window.ax.get_xlim()
            ylimCurrent = self.window.ax.get_ylim()
            ylimMax = self.getExtrema(self.window.ax.lines)

            def findLimit(x, y):
                return [new if new is not None else curr
                        for new, curr in zip(x, y)]

            xlim = findLimit(xlim, xlimCurrent)
            ylim = findLimit(ylim, ylimMax)
            ylim = findLimit(ylim, ylimCurrent)

            if save:
                limits = [xlim, ylim]
                ind = self.comboLimits.count()
                self.comboLimits.insertItem(ind, label, (limits,))

            self.cbAutoUpdate.setChecked(False)
            self.window.ax.set_xlim(xlim)
            self.window.ax.set_ylim(ylim)
            self.window.updateTicks()
            self.window.canvas.draw()


    def saveAxesLimits(self):
        label, ok = QtGui.QInputDialog.getText(self,
                                               'Save axes limits',
                                               'Insert label:')
        if ok:
            xlim = self.window.ax.get_xlim()
            ylim = self.window.ax.get_ylim()
            limits = [xlim, ylim]

            ind = self.comboLimits.count()
            self.comboLimits.insertItem(ind, label, (limits,))


    def pickThreshold(self):
        self.removeSpikeMarkers()
        self.picker = ThresholdPicker(self)


    def markSpikes(self, max=None, min=None):
        """
            Marks spikes with a vertical line.
            If max/min is specified, all datapoints above/below that value will
            be marked.
            Else, a spike detection algorithm will find spikes and the maximum
            of the spike will be marked
        """
        data = self.window.data
        spikes = []
        if max is not None or min is not None:
            for sensor, sensorData in data.iteritems():
                if self.window.graphs[sensor].get_visible():
                    x, y = sensorData
                    if max is not None:
                        newSpikes = [_x for _x, _y in zip(x, y) if _y >= max]
                        spikes.extend()
                    if min is not None:
                        newSpikes = [_x for _x, _y in zip(x, y) if _y <= max]
                        spikes.extend(newSpikes)
        else:
            print('Spike detection algorithm not implemented yet')
            return

        for spike in spikes:
            self.window.spikes.append(self.window.ax.axvline(spike, 0, 1,
                                                             color='red',
                                                             alpha=0.5,
                                                             zorder=-1))

        self.window.canvas.draw()


    def removeSpikeMarkers(self):
        for spike in self.window.spikes:
            spike.remove()
        self.window.spikes = []
        self.window.canvas.draw()


    def applyAxesLimits(self):
        combo = self.comboLimits
        limits = combo.itemData(combo.currentIndex()).toPyObject()[0]

        if limits == 'showAll':
            self.zoom('all')
            return
        if limits == 'showAllY':
            self.zoom('y')
            return
        if limits == 'showAllX':
            self.zoom('x')
            return

        xlim, ylim = limits

        self.cbAutoUpdate.setChecked(False)
        self.window.ax.set_xlim(xlim)
        self.window.ax.set_ylim(ylim)
        self.window.updateTicks()
        self.window.canvas.draw()


    def clearMessage(self):
        msg = QtGui.QMessageBox()
        msg.setWindowTitle('Are you sure?')
        msg.setText('Do you really want to clear the canvas of its graphs?' +
                    ' Steady state detection will not be affected.')
        msg.setStandardButtons(QtGui.QMessageBox.Cancel | QtGui.QMessageBox.Ok)
        msg.setDefaultButton(QtGui.QMessageBox.Cancel)
        msg.setWindowState(msg.windowState() & ~QtCore.Qt.WindowMinimized |
                           QtCore.Qt.WindowActive)
        msg.activateWindow()

        answer = msg.exec_()
        if answer == QtGui.QMessageBox.Ok:
            self.window.clear()


    def changeSteadyStateDefinition(self):
        threshold, timerange, ok = SteadyStateDialog.getDefinition(self)
        if ok:
            self.steadyStateThreshold = float(threshold)
            self.steadyStateTimeRange = float(timerange)
            self.updateSteadyStateText()
        else:
            self.statusbar.showMessage('Did not set new definition', 5000)


    def updateSteadyStateText(self):
        text = (u'Steady state definition: \u0394T\u2264 ' +
                u'<span style="font-weight:bold">{}\u00b0C '
                .format(self.steadyStateThreshold) +
                u'in {} minutes</span>.'.format(self.steadyStateTimeRange))
        self.statusSteadyState.setText(text)


    def updateTimeZoneText(self):
        if self.timeZone >= 0:
            dtz = '+' + str(self.timeZone)
        else:
            dtz = self.timeZone
        text = ('Your timezone is ' +
                '<span style="font-weight:bold">CET{}</span>'
                .format(dtz))
        self.statusTimeZone.setText(text)
        self.statusTimeZone.update()


    def pickTimeZone(self):
        tzDialog = QtGui.QInputDialog(self)
        tz, ok = tzDialog.getText(self,
                                  'Insert your time zone',
                                  'Your offset from CET in hours:',
                                  QtGui.QLineEdit.Normal,
                                  str(self.timeZone))
        if ok:
            tz = int(tz)
            tzDelta = tz - self.timeZone
            self.timeZone = tz
            self.window.applyTimeZone(tzDelta)
            self.zoom('all')
            self.updateTimeZoneText()


    def applySteadyState(self, sensorName, steady):
        if steady:
            result = self.highlightSteadySensor(sensorName)
        else:
            result = self.removeHighlighting(sensorName)

        if result:
            sensor = self.window.getSensor(sensorName)
            sensor.steady = steady


    def highlightSteadySensor(self, sensor):
        table = self.sensorTable
        if table.rowCount() < 1:
            return False

        item = table.findItems(sensor, QtCore.Qt.MatchExactly)[0]
        row = item.row()

        # In case a subsystem has the same name as a sensor. Only mark sensors
        if item.column() != 3:
            return False

        for i in range(table.columnCount()):
            item = table.item(row, i)
            if item is None:
                continue
            item.setBackground(QtCore.Qt.green)
            item.setTextColor(QtCore.Qt.white)

        return True


    def removeHighlighting(self, sensor):
        table = self.sensorTable
        if table.rowCount() < 1:
            return False

        item = table.findItems(sensor, QtCore.Qt.MatchExactly)[0]
        row = item.row()
        # In case a subsystem has the same name as a sensor. Only mark sensors
        if item.column() != 3:
            return False

        for i in range(table.columnCount()):
            item = table.item(row, i)
            if item is None:
                continue
            item.setBackground(QtCore.Qt.white)
            item.setTextColor(QtCore.Qt.black)

        return True


    def activateZoom(self):
        if self.toolbar._active == 'ZOOM':
            self.statusbar.showMessage("Zoom already active", 3000)
            return
        self.toolbar.zoom()


    def activatePan(self):
        if self.toolbar._active == 'PAN':
            self.statusbar.showMessage("Pan already active", 3000)
            return
        self.toolbar.pan()


    def deactivatePanZoom(self):
        if self.toolbar._active == 'PAN':
            self.toolbar.pan()
        if self.toolbar._active == 'ZOOM':
            self.toolbar.zoom()


    def refresh(self):
        graphs = self.window.graphs.values()

        showLines = self.cbShowLines.isChecked()
        if showLines:
            for graph in graphs:
                mpl.artist.setp(graph, linestyle='solid')
        else:
            for graph in graphs:
                mpl.artist.setp(graph, linestyle='none')

        showMarkers = self.cbShowMarkers.isChecked()
        if showMarkers:
            for graph in graphs:
                mpl.artist.setp(graph, marker=self.window.marker)
        else:
            for graph in graphs:
                mpl.artist.setp(graph, marker=None, markerfacecolor=None,
                                markeredgecolor=None)

        showGrid = self.cbShowGrid.isChecked()
        if showGrid:
            self.window.ax.grid(True)
        else:
            self.window.ax.grid(False)

        self.window.canvas.draw()


    def getVisibleLines(self):
        ax = self.window.ax
        visLines = []
        for line in ax.lines:
            if line.get_visible():
                visLines.append(line)
        return visLines


    def compareTo(self):
        quantity = self.comboCompareTo.currentText()
        self.window.setCompareAxis(quantity)


    def zoom(self, toShow, ax=None):
        xtot = []
        ytot = []

        if ax is None:
            ax = self.window.ax

        visLines = self.getVisibleLines()

        if len(visLines) == 0:
            return

        for line in visLines:
            x = line.get_xdata()
            y = line.get_ydata()
            xtot.extend(x)
            ytot.extend(y)

        ypad = abs(max(ytot) - min(ytot)) * 0.05
        xpad = abs(max(xtot) - min(xtot)) * 0.05

        xmin = min(xtot) - xpad
        xmax = max(xtot) + xpad
        ymin = min(ytot) - ypad
        ymax = max(ytot) + ypad

        if toShow == 'all':
            self.window.updateTicks(xmin=xmin, xmax=xmax)
            ax.set_xlim([xmin, xmax])
            ax.set_ylim([ymin, ymax])

        elif toShow == 'x':
            self.window.updateTicks(xmin=xmin, xmax=xmax)
            ax.set_xlim([xmin, xmax])

        elif toShow == 'y':
            ax.set_ylim([ymin, ymax])

        self.window.canvas.draw()


    def saveFigure(self):
        if not os.path.exists('img'):
            os.makedirs('img')
        fileName = 'img/THM_monitor_' + str(datetime.datetime.now()) + '.svg'
        self.window.fig.savefig(fileName, format='svg')
        self.statusbar.showMessage('Figure successfully saved to img/', 5000)


    def testAlarm(self):
        self.warning(1)


    @pyqtSlot('PyQt_PyObject', 'PyQt_PyObject')
    def showFlashScreen(self, on, color=None):
        self.originalColor = 'white'
        if on:
            self.originalColor = self.window.ax.get_axis_bgcolor()
            self.window.ax.set_axis_bgcolor(color)
            self.window.ax.figure.canvas.draw()
        if not on:
            self.window.ax.set_axis_bgcolor(self.originalColor)
            self.window.ax.figure.canvas.draw()


    def stopAlarm(self):
        self.alarmThread.terminate()
        self.window.ax.set_axis_bgcolor(self.originalColor)
        self.window.canvas.draw()


    def warning(self, level):
        return
        if level == 1:
            self.alarmThread.level = level
            self.alarmThread.start()
            self.showWarningPopup()
        if level == 2:
            self.alarmThread.level = level
            self.alarmThread.start()
            self.showCriticalPopup()


    def averagePlot(self):
        points, method, ok = AverageDialog.getSettings(self)
        if ok:
            self.window.updateAverage(points, method)


    def restorePlot(self):
        self.window.restore()


    def populateSensorTable(self, sensors):
        logging.info("Populating table")
        table = self.sensorTable
        colors = self.window.colors
        self.checkBoxes = {}
        self.patches = {}
        self.tempLabels = {}
        rowCount = 0

        size = len(sensors)

        # Add subsystems
        uniqueSubsystems = list(set([sensor.subsystem for sensor in sensors]))
        subsystems = sorted(uniqueSubsystems)
        for subsystem in subsystems:
            rowPos = table.rowCount()
            table.insertRow(rowPos)
            rowCount += 1

            # Checkboxes
            cb = QtGui.QCheckBox()
            self.checkBoxes['subsystem' + subsystem] = cb

            # Make checkboxes 0 or 1 only and check them
            cb.setTristate(False)
            cb.setChecked(True)
            cb.setMaximumWidth(20)
            cb.stateChanged.connect(
                functools.partial(self.toggleSubsystem, subsystem, cb))
            cb.setStyleSheet('QCheckBox {background-color: rgb(153,153,153)}')
            cb.setToolTip('<span style="color:black;">' +
                          'Toggle all sensors in this subsystem</span>')

            # Subsystem names
            item = QtGui.QTableWidgetItem()
            item.setText(subsystem)
            item.setBackground(QtCore.Qt.white)
            item.setTextColor(QtCore.Qt.black)
            font = QtGui.QFont()
            font.setBold(True)
            item.setFont(font)
            table.setItem(rowPos, 1, item)

            table.setCellWidget(rowPos, 0, cb)

        # Add components
        for subsystem in subsystems:
            components = [s.component for s in sensors
                          if s.subsystem == subsystem]
            components = sorted(list(set(components)))
            for component in components:
                itemList = table.findItems(subsystem, QtCore.Qt.MatchExactly)
                # Check if the found subsystem name is in column 1.
                # Important in case there is a component that has the name of
                # a subsystem. If there is more than one subsystem with the
                # same name, choose the first one.
                for item in itemList:
                    if item.column() == 1:
                        rowPos = item.row() + 1
                        break
                table.insertRow(rowPos)
                rowCount += 1

                # Checkboxes
                cb = QtGui.QCheckBox()
                self.checkBoxes['component' + component] = cb

                # Make checkboxes 0 or 1 only and check them
                cb.setTristate(False)
                cb.setChecked(True)
                cb.setMaximumWidth(20)
                cb.stateChanged.connect(
                    functools.partial(self.toggleComponent, component, cb))
                style = 'QCheckBox {background-color: rgb(153,153,153)}'
                cb.setStyleSheet(style)
                cb.setToolTip('<span style="color:black;">' +
                              'Toggle all sensors in this component</span>')

                # Component names
                item = QtGui.QTableWidgetItem()
                item.setText(component)
                item.setBackground(QtCore.Qt.white)
                item.setTextColor(QtCore.Qt.black)
                font = QtGui.QFont()
                font.setBold(True)
                item.setFont(font)
                table.setItem(rowPos, 2, item)

                table.setCellWidget(rowPos, 0, cb)


        # Add sensors
        self.startTrackingProgress()
        sensors = sorted(sensors, key=lambda x: x.name, reverse=True)
        for i, sensor in enumerate(sensors):
            self.trackProgress(size, i,
                               'Populating sensor table (sensor {} of {})'
                               .format(i, size))

            # First find the subsystem and memorize row.
            itemList = table.findItems(sensor.subsystem,
                                       QtCore.Qt.MatchExactly)
            for item in itemList:
                if item.column() == 1:
                    subsystemRow = item.row()
                    break
            itemList = table.findItems(sensor.component,
                                       QtCore.Qt.MatchExactly)
            # Check if the found component name is in column 2.
            # Important in case there is a sensor that has the name of
            # a component or subsystem.  If there is more than one component
            # with the same name, choose the first one.
            # Also, only consider components below this subsystem
            for item in itemList:
                if item.column() == 2 and item.row() > subsystemRow:
                    rowPos = item.row() + 1
                    break
            table.insertRow(rowPos)
            rowCount += 1

            tempText = QtGui.QTableWidgetItem()
            tempText.setText(sensor.name)
            tempText.setBackground(QtCore.Qt.white)
            tempText.setTextColor(QtCore.Qt.black)

            color = colors[sensor.name]
            patch = ColorPatch(color)
            self.patches[sensor.name] = patch
            patch.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            patch.clicked.connect(functools.partial(self.changeColor,
                                                    sensor.name))

            # Checkboxes
            cb = QtGui.QCheckBox()
            self.checkBoxes[sensor.name] = cb

            # Current temperature value labels
            currTempText = QtGui.QTableWidgetItem()
            currTempText.setBackground(QtCore.Qt.white)
            currTempText.setTextColor(QtCore.Qt.black)
            self.tempLabels[sensor.name] = currTempText

            # Make checkboxes 0 or 1 only and check them
            cb.setTristate(False)
            cb.setChecked(True)
            cb.setMaximumWidth(20)

            cb.stateChanged.connect(self.togglePlots)

            if not fileMode:
                tip = ('<span style="color:black;">'
                       'Green = steady state reached</span>')
                tempText.setToolTip(tip)
                currTempText.setToolTip(tip)
            tip = '<span style="color:black;">Click to change color</span>'
            patch.setToolTip(tip)
            cb.setStyleSheet('QCheckBox {background-color: rgb(153,153,153)}')

            # Add widgets to row
            table.setCellWidget(rowPos, 0, cb)
            table.setCellWidget(rowPos, 1, patch)
            table.setItem(rowPos, 2, currTempText)
            table.setItem(rowPos, 3, tempText)

        table.setRowCount(rowCount)
        self.stopTrackingProgress()


    def getSelectedSensors(self):
        return [sensor
                for sensor, cb in self.checkBoxes.iteritems()
                if cb.isChecked()]


    def logMissingBeacon(self, time):
        logging.error('Unusually long gap between beacons. ' +
                      'Check if one is missing right after {}'.format(time))


    def changeColor(self, sensor):
        patch = self.patches[sensor]
        graph = self.window.graphs[sensor]

        # Choose new color
        dialog = QtGui.QColorDialog()
        dialog.setWindowState(dialog.windowState() &
                              ~QtCore.Qt.WindowMinimized |
                              QtCore.Qt.WindowActive)
        dialog.activateWindow()
        color = dialog.getColor()
        if not QColor.isValid(color):
            logging.error('Picked color is not valid')
            return

        # Conversion to matplotlib-compatible rgb value
        red = color.red() / 255.
        blue = color.blue() / 255.
        green = color.green() / 255.
        rgb = (red, green, blue)

        # Update colors
        patch.setColor(color)
        graph.set_color(rgb)

        self.window.colors[sensor] = rgb

        # Re-draw canvas
        self.window.canvas.draw()


    def toggleAllPlots(self, activateAll=None):
        if activateAll is not None:
            for cb in self.checkBoxes.values():
                cb.blockSignals(True)
                cb.setChecked(activateAll)
                cb.blockSignals(False)
            self.togglePlots()


    def toggleSubsystem(self, sub, cb):
        checked = cb.isChecked()
        for sensor in self.window.sensors:
            if sensor.subsystem == sub:
                self.checkBoxes[sensor.name].blockSignals(True)
                self.checkBoxes[sensor.name].setChecked(checked)
                self.checkBoxes[sensor.name].blockSignals(False)
        self.togglePlots()


    def toggleComponent(self, comp, cb):
        checked = cb.isChecked()
        for sensor in self.window.sensors:
            if sensor.component == comp:
                self.checkBoxes[sensor.name].blockSignals(True)
                self.checkBoxes[sensor.name].setChecked(checked)
                self.checkBoxes[sensor.name].blockSignals(False)
        self.togglePlots()


    def togglePlots(self):
        selectedSensors = self.getSelectedSensors()
        self.window.toggleSensors(selectedSensors)


    def createWindow(self, ):
        self.window = Window(self)
        windowLayout = QtGui.QVBoxLayout()
        windowLayout.addWidget(self.window.canvas)
        self.windowContainer.setLayout(windowLayout)


    def startFeed(self):
        self.window.goLive()
        self.statusbar.showMessage('Feed started', 2000)


    def stopFeed(self):
        self.window.stahp()



##############################################################################
# Misc Classes
##############################################################################
class Sensor():
    # Mapping should be provided before initializing instances via
    # Sensor.mapping = dict
    mapping = {}

    def __init__(self, name):
        self.name = name
        if name in self.mapping:
            self.subsystem = self.mapping[name]['subsystem']
            self.component = self.mapping[name]['component']
        else:
            print("No component/subsystem found for sensor {}. ".format(name) +
                  "Make sure this sensor has a mapping in the supplied " +
                  "mapping dict")
            self.subsystem = 'None'
            self.component = 'None'
        self.steady = False



class MyHeader(QHeaderView):
    """
    QHeaderView subclass featuring a master checkbox
    """
    isOn = True

    def __init__(self, orientation, labels, gui):
        parent = gui.sensorTable
        QHeaderView.__init__(self, orientation, parent)
        self.gui = gui
        self.labels = labels


    def paintSection(self, painter, rect, logicalIndex):
        painter.save()
        color = QtGui.QColor(153, 153, 153)
        QHeaderView.paintSection(self, painter, rect, logicalIndex)
        painter.restore()
        painter.fillRect(rect, QtGui.QBrush(color))
        pen = QtGui.QPen(QtCore.Qt.white)
        painter.setPen(pen)
        font = QtGui.QFont('Helvetica', 10, QtGui.QFont.Bold)
        painter.setFont(font)
        painter.drawText(rect, QtCore.Qt.AlignLeft, self.labels[logicalIndex])

        if logicalIndex == 0:
            option = QStyleOptionButton()
            option.rect = QRect(0, 7, 10, 10)
            if self.isOn:
                option.state = QStyle.State_On
            else:
                option.state = QStyle.State_Off
            self.style().drawControl(QStyle.CE_CheckBox, option, painter)
            self.option = option


    def mousePressEvent(self, event):
        x = event.pos().x()
        y = event.pos().y()
        inxRange = 5 < x < 17
        inyRange = 7 < y < 21
        if inxRange and inyRange:
            self.isOn = not self.isOn
            self.updateSection(0)
            QHeaderView.mousePressEvent(self, event)
            self.gui.toggleAllPlots(self.isOn)



class Plot():
    def __init__(self, line, name, colormap):
        self.name = str(name)
        self.colormap = str(colormap)
        self.color = mpl.colors.colorConverter.to_rgb(mpl.artist.getp(line,
                                                                      'color'))
        self.linestyle = str(mpl.artist.getp(line, 'linestyle'))
        self.linewidth = str(int(mpl.artist.getp(line, 'linewidth')))
        self.marker = str(mpl.artist.getp(line, 'marker'))



class ThresholdPicker():
    """
        Threshold picker
    """
    def __init__(self, gui):
        self.window = gui.window
        self.gui = gui
        self.threshLines = []
        self.thresholds = []
        self.crossVert = None
        self.crossHor = None
        canvas = self.window.fig.canvas
        self._getter = canvas.mpl_connect('button_press_event', self.getThresh)
        self._setter = canvas.mpl_connect('key_press_event', self.setThresh)
        self._cursorSetter = canvas.mpl_connect('axes_enter_event',
                                                self.setCursor)


    def __del__(self):
        self.window.fig.canvas.mpl_disconnect(self._getter)
        self.window.fig.canvas.mpl_disconnect(self._setter)
        self.window.fig.canvas.mpl_disconnect(self._cursorSetter)


    def setCursor(self, event=None):
        QtGui.QApplication.setOverrideCursor(QtCore.Qt.SplitVCursor)


    def restoreCursor(self, event=None):
        QtGui.QApplication.restoreOverrideCursor()


    def updateCursor(self, x, y):
        try:
            self.crossHor.remove()
        except AttributeError:
            pass
        self.crossHor = self.window.ax.axhline(y, 0, 1, color='black')
        self.window.canvas.draw()


    def setThresh(self, event):
        if self.crossHor is not None:
            y = max(self.crossHor.get_ydata())

        if event.key == 'enter':
            if y in self.thresholds:
                return
            self.thresholds.append(y)
            self.threshLines.append(self.window.ax.axhline(y, 0, 1,
                                                           color='black'))
            self.window.canvas.draw()

        if len(self.thresholds) > 1:
            self.finish()


    def getThresh(self, event):
        x = event.xdata
        y = event.ydata
        if event.button == 3:
            self.finish()
        elif event.button == 1:
            self.updateCursor(x, y)


    def finish(self, ):
        """
            Ends threshold picking and delegates thresholds to
            markSpikes(). If only one threshold was selected, the absolute
            value of it serves as the upper threshold and the negative absolute
            serves as the lower threshold.
        """
        thresholds = self.thresholds
        if len(thresholds) > 0:
            self.crossHor.remove()

            for line in self.threshLines:
                line.remove()

            if len(thresholds) == 1:
                thresh = thresholds[0]
                self.gui.markSpikes(max=abs(thresh), min=-abs(thresh))
            elif len(thresholds) == 2:
                self.gui.markSpikes(max=max(thresholds), min=min(thresholds))

            self.window.canvas.draw()
            self.gui.activatePan()
            self.restoreCursor()

        self.window.fig.canvas.mpl_disconnect(self._getter)
        self.window.fig.canvas.mpl_disconnect(self._setter)
        self.window.fig.canvas.mpl_disconnect(self._cursorSetter)
        del self



class Combo(QtGui.QComboBox):
    def __init__(self, parent=None):
        super(Combo, self).__init__(parent=None)

        self.familywide = False
        self.isGlobal = False
        self.attribute = None
        self.plotName = None



class ColorPatch(QtGui.QWidget):
    clicked = QtCore.pyqtSignal()

    def __init__(self, color):
        super(ColorPatch, self).__init__()
        self.color = mpl.colors.rgb2hex(color)


    def mousePressEvent(self, event):
        self.clicked.emit()


    def paintEvent(self, event):
        painter = QPainter()
        painter.begin(self)
        self.drawPatch(painter)
        painter.end()


    def drawPatch(self, painter):
        painter.setBrush(QColor(self.color))
        painter.drawRect(10, 10, 50, 10)


    def setColor(self, color, type=None):
        if type == 'rgb':
            rgb = color
            color = QtGui.QColor()
            color.setRgb(*rgb)
        self.color = color
        self.update()


##############################################################################
# Main Application Loop
##############################################################################
if __name__ == '__main__':
    app = QtGui.QApplication(sys.argv)
    main = Monitor()
    main.show()

    sys.exit(app.exec_())
