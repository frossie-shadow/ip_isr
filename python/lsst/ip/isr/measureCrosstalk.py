#
# LSST Data Management System
# Copyright 2008-2017 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
"""
Measure intra-CCD crosstalk coefficients.
"""
from __future__ import absolute_import, division, print_function

__all__ = ["extractCrosstalkRatios", "measureCrosstalkCoefficients",
           "MeasureCrosstalkConfig", "MeasureCrosstalkTask"]

from builtins import range

import itertools
import numpy as np

from lsst.afw.detection import FootprintSet, Threshold
from lsst.pex.config import Config, Field, ListField, ConfigurableField
from lsst.pipe.base import CmdLineTask

from .crosstalk import calculateBackground, extractAmp
from .isrTask import IsrTask


def extractCrosstalkRatios(exposure, threshold=30000, badPixels=["SAT", "BAD", "INTRP"]):
    """Extract crosstalk ratios between different amplifiers

    For pixels above ``threshold``, we calculate the ratio between each
    target amp and source amp. We return a list of ratios for each pixel
    for each target/source combination, as a matrix of lists.

    Parameters
    ----------
    exposure : `lsst.afw.image.Exposure`
        Exposure for which to measure crosstalk.
    threshold : `float`
        Lower limit on pixels for which we measure crosstalk.
    badPixels : `list` of `str`
        Mask planes indicating a pixel is bad.

    Returns
    -------
    ratios : `list` of `list` of `list` of `float`
        A matrix of lists of ratios. ``ratios[i][j]`` is a list of
        the fraction of the ``j``-th amp present on the ``i``-th amp.
    """
    mi = exposure.getMaskedImage()
    FootprintSet(mi, Threshold(threshold), "DETECTED")
    detected = mi.getMask().getPlaneBitMask("DETECTED")
    bad = mi.getMask().getPlaneBitMask(badPixels)
    bg = calculateBackground(mi, badPixels + ["DETECTED"])

    ccd = exposure.getDetector()

    ratios = [[[] for iAmp in ccd] for jAmp in ccd]

    for ii, iAmp in enumerate(ccd):
        iImage = mi.Factory(mi, iAmp.getBBox())
        iMask = iImage.getMask().getArray()
        select = (iMask & detected > 0) & (iMask & bad == 0) & np.isfinite(iImage.getImage().getArray())
        for jj, jAmp in enumerate(ccd):
            if ii == jj:
                continue
            jImage = extractAmp(mi.getImage(), jAmp, iAmp.getReadoutCorner())
            pixels = (jImage.getArray()[select] - bg)/iImage.getImage().getArray()[select]
            ratios[jj][ii] += pixels.tolist()

    return ratios


def measureCrosstalkCoefficients(ratios, rejIter=3, rejSigma=2.0):
    """Measure crosstalk coefficients from the ratios

    Given a list of ratios for each target/source amp combination,
    we measure a robust mean and error.

    Parameters
    ----------
    ratios : `list` of `list` of `list` of `float`
        Matrix of lists of ratios.
    rejIter : `int`
        Number of rejection iterations.
    rejSigma : `float`
        Rejection threshold (sigma).

    Returns
    -------
    coeff : `numpy.ndarray`
        Crosstalk coefficients.
    coeffErr : `numpy.ndarray`
        Crosstalk coefficient errors.
    coeffNum : `numpy.ndarray`
        Number of pixels for each measurement.
    """
    numAmps = len(ratios)
    assert all(len(rr) == numAmps for rr in ratios)

    coeff = np.zeros((numAmps, numAmps))
    coeffErr = np.zeros((numAmps, numAmps))
    coeffNum = np.zeros((numAmps, numAmps), dtype=int)

    for ii, jj in itertools.product(range(numAmps), range(numAmps)):
        if ii == jj:
            continue
        values = np.array(ratios[ii][jj])
        values = values[np.abs(values) < 1.0]  # Discard unreasonable values
        for rej in range(rejIter):
            lo, med, hi = np.percentile(values, [25.0, 50.0, 75.0])
            sigma = 0.741*(hi - lo)
            good = np.abs(values - med) < rejSigma*sigma
            print(ii, jj, rej, med, sigma, len(values), good.sum())
            if good.sum() == len(good):
                break
            values = values[good]

        coeff[ii][jj] = np.mean(values)
        coeffErr[ii][jj] = np.std(values)
        coeffNum[ii][jj] = len(values)

    return coeff, coeffErr, coeffNum


class MeasureCrosstalkConfig(Config):
    """Configuration for MeasureCrosstalkTask"""
    isr = ConfigurableField(target=IsrTask, doc="Instrument signature removal")
    threshold = Field(dtype=float, default=30000, doc="Minimum level for which to measure crosstalk")
    badMask = ListField(dtype=str, default=["SAT", "BAD", "INTRP"], doc="Mask planes to ignore")
    rejIter = Field(dtype=int, default=3, doc="Number of rejection iterations")
    rejSigma = Field(dtype=float, default=2.0, doc="Rejection threshold (sigma)")

    def setDefaults(self):
        Config.setDefaults(self)
        self.isr.doWrite = False
        self.isr.growSaturationFootprintSize = 0  # We want the saturation spillover: it's good signal


class MeasureCrosstalkTask(CmdLineTask):
    """Measure intra-CCD crosstalk

    This Task behaves in a scatter-gather fashion:
    * Scatter: get ratios for each CCD.
    * Gather: combine ratios to produce crosstalk coefficients.
    """
    ConfigClass = MeasureCrosstalkConfig
    _DefaultName = "measureCrosstalk"

    def __init__(self, *args, **kwargs):
        CmdLineTask.__init__(self, *args, **kwargs)
        self.makeSubtask("isr")

    @classmethod
    def _makeArgumentParser(cls):
        parser = super(MeasureCrosstalkTask, cls)._makeArgumentParser()
        parser.add_argument("--dump-ratios", dest="dumpRatios",
                            help="Name of pickle file to which to write crosstalk ratios")
        return parser

    @classmethod
    def parseAndRun(cls, *args, **kwargs):
        """Implement scatter/gather"""
        kwargs["doReturnResults"] = True
        results = super(MeasureCrosstalkTask, cls).parseAndRun(*args, **kwargs)
        task = cls(config=results.parsedCmd.config, log=results.parsedCmd.log)
        resultList = [rr.result for rr in results.resultList]
        if results.parsedCmd.dumpRatios:
            import pickle
            pickle.dump(resultList, open(results.parsedCmd.dumpRatios, "w"))
        return task.reduce(resultList)

    def run(self, dataRef):
        """Get crosstalk ratios for CCD

        Parameters
        ----------
        dataRef : `lsst.daf.peristence.ButlerDataRef`
            Data reference for CCD.

        Returns
        -------
        ratios : `list` of `list` of `list` of `float`
            A matrix of lists of ratios.
        """
        exposure = self.isr.runDataRef(dataRef).exposure
        ratios = extractCrosstalkRatios(exposure, self.config.threshold, list(self.config.badMask))
        self.log.info("Extracted %d pixels from %s", sum(len(jj) for ii in ratios for jj in ii),
                      dataRef.dataId)
        return ratios

    def reduce(self, ratioList):
        """Combine ratios to produce crosstalk coefficients

        Parameters
        ----------
        ratioList : `list` of `list` of `list` of `list` of `float`
            A list of matrices of lists of ratios.

        Returns
        -------
        coeff : `numpy.ndarray`
            Crosstalk coefficients.
        coeffErr : `numpy.ndarray`
            Crosstalk coefficient errors.
        """
        numAmps = len(ratioList[0])
        assert all(len(rr) == numAmps for rr in ratioList)
        assert all(all(len(xx) == numAmps for xx in rr) for rr in ratioList)
        ratios = [[sum((rr[ii][jj] for rr in ratioList), []) for jj in range(numAmps)]
                  for ii in range(numAmps)]
        coeff, coeffErr, coeffNum = measureCrosstalkCoefficients(ratios, self.config.rejIter,
                                                                 self.config.rejSigma)
        self.log.info("Coefficients:\n%s\n", coeff)
        self.log.info("Errors:\n%s\n", coeffErr)
        self.log.info("Numbers:\n%s\n", coeffNum)
        return coeff, coeffErr, coeffNum

    def _getConfigName(self):
        """Disable config output"""
        return None

    def _getMetadataName(self):
        """Disable metdata output"""
        return None
