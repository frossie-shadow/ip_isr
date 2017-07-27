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
Apply intra-CCD crosstalk corrections
"""
from __future__ import absolute_import, division, print_function

__all__ = ["CrosstalkConfig", "CrosstalkTask", "subtractCrosstalk"]

from builtins import zip

import numpy as np

import lsst.afw.math
import lsst.afw.table
import lsst.afw.detection

from lsst.pex.config import Config, Field, ListField
from lsst.pipe.base import Task

import lsst.afw.display
display = lsst.afw.display.Display(backend="ds9", frame=1)


class CrosstalkConfig(Config):
    """Configuration for intra-CCD crosstalk removal"""
    minPixelToMask = Field(dtype=float, default=45000,
                           doc="Set crosstalk mask plane for pixels over this value")
    crosstalkMaskPlane = Field(dtype=str, default="CROSSTALK", doc="Name for crosstalk mask plane")
    coeffs = ListField(
        dtype=float,
        doc=("Flattened crosstalk coefficient matrix; should have nAmps x nAmps entries. "
             "Once 'reshape'-ed, ``coeffs[i][j]`` is the fraction of the j-th amp present on the i-th amp."),
        default=[0, 0, 0, 0,
                 0, 0, 0, 0,
                 0, 0, 0, 0,
                 0, 0, 0, 0],
    )

    def getCoeffs(self, numAmps):
        """Return a 2-D numpy array of crosstalk coefficients of the proper shape"""
        return np.array(self.coeffs).reshape((numAmps, numAmps))


class CrosstalkTask(Task):
    """Apply intra-CCD crosstalk correction"""
    ConfigClass = CrosstalkConfig

    def run(self, exposure):
        """Apply intra-CCD crosstalk correction

        Parameters
        ----------
        exposure : `lsst.afw.image.Exposure`
            Exposure for which to remove crosstalk.
        """
        self.log.info("Applying crosstalk correction")
        numAmps = len(exposure.getDetector())
        subtractCrosstalk(exposure, self.config.getCoeffs(numAmps), minPixelToMask=self.config.minPixelToMask,
                          crosstalkStr=self.config.crosstalkMaskPlane)


# Flips required to get the corner to the lower-left
# (an arbitrary choice; flips are relative, so the choice of reference here is not important)
X_FLIP = {lsst.afw.table.LL: False, lsst.afw.table.LR: True,
              lsst.afw.table.UL: False, lsst.afw.table.UR: True}
Y_FLIP = {lsst.afw.table.LL: False, lsst.afw.table.LR: False,
              lsst.afw.table.UL: True, lsst.afw.table.UR: True}


def extractAmp(image, amp, corner):
    """Return an image of the amp

    The returned image will have the amp's readout corner in the
    nominated `corner`.

    Parameters
    ----------
    image : `lsst.afw.image.Image` or `lsst.afw.image.MaskedImage`
        Image containing the amplifier of interest.
    amp : `lsst.afw.table.AmpInfoRecord`
        Amplifier information.
    corner : `lsst.afw.table.ReadoutCorner` or `None`
        Corner in which to put the amp's readout corner, or `None` for
        no flipping.

    Returns
    -------
    output : `lsst.afw.image.Image`
        Image of the amplifier in the standard configuration.
    """
    output = image.Factory(image, amp.getBBox())
    ampCorner = amp.getReadoutCorner()
    # Flipping is necessary only if the desired configuration doesn't match what we currently have
    xFlip = X_FLIP[corner] ^ X_FLIP[ampCorner]
    yFlip = Y_FLIP[corner] ^ Y_FLIP[ampCorner]
    return lsst.afw.math.flipImage(output, xFlip, yFlip)


def calculateBackground(mi, badPixels=["BAD"]):
    """Calculate median background in image

    Getting a great background model isn't important for crosstalk correction,
    since the crosstalk is at a low level. The median should be sufficient.

    Parameters
    ----------
    mi : `lsst.afw.image.MaskedImage`
        MaskedImage for which to measure background.
    badPixels : `list` of `str`
        Mask planes to ignore.

    Returns
    -------
    bg : `float`
        Median background level.
    """
    mask = mi.getMask()
    stats = lsst.afw.math.StatisticsControl()
    stats.setAndMask(mask.getPlaneBitMask(badPixels))
    return lsst.afw.math.makeStatistics(mi, lsst.afw.math.MEDIAN, stats).getValue()


def subtractCrosstalk(exposure, coeffs, badPixels=["BAD"], minPixelToMask=45000, crosstalkStr="CROSSTALK"):
    """Subtract the intra-CCD crosstalk from an exposure

    We set the mask plane indicated by ``crosstalkStr`` in a target amplifier
    for pixels in a source amplifier that exceed `minPixelToMask`. Note that
    the correction is applied to all pixels in the amplifier, but only those
    that have a substantial crosstalk are masked with ``crosstalkStr``.

    The uncorrected image is used as a template for correction. This is good
    enough if the crosstalk is small (e.g., coefficients < ~ 1e-3), but if it's
    larger you may want to iterate.

    Parameters
    ----------
    exposure : `lsst.afw.image.Exposure`
        Exposure for which to subtract crosstalk.
    coeffs : `numpy.ndarray`
        Matrix of crosstalk coefficients. ``coeffs[i][j]`` is the fraction
        of the j-th amp present on the i-th amp.
    badPixels : `list` of `str`
        Mask planes to ignore.
    minPixelToMask : `float`
        Minimum pixel value in source amplifier for which to set
        ``crosstalkStr`` mask plane in target amplifier.
    crosstalkStr : `str`
        Mask plane name for pixels greatly modified by crosstalk.
    """
    mi = exposure.getMaskedImage()
    mask = mi.getMask()
    bg = calculateBackground(mi, badPixels)

    ccd = exposure.getDetector()
    numAmps = len(ccd)
    assert coeffs.shape == (numAmps, numAmps)

    # Set the crosstalkStr bit for the bright pixels (those which will have significant crosstalk correction)
    crosstalkPlane = mask.addMaskPlane(crosstalkStr)
    lsst.afw.display.getDisplay().setMaskPlaneColor(crosstalkStr, lsst.afw.display.MAGENTA)
    footprints = lsst.afw.detection.FootprintSet(mi, lsst.afw.detection.Threshold(minPixelToMask))
    footprints.setMask(mask, crosstalkStr)
    crosstalk = mask.getPlaneBitMask(crosstalkStr)

    subtrahend = mi.Factory(mi.getBBox())
    subtrahend.set((0, 0, 0))
    for ii, iAmp in enumerate(ccd):
        iImage = subtrahend.Factory(subtrahend, iAmp.getBBox())
        for jj, jAmp in enumerate(ccd):
            if ii == jj:
                assert coeffs[ii, jj] == 0.0
            if coeffs[ii, jj] == 0.0:
                continue

            jImage = extractAmp(mi, jAmp, iAmp.getReadoutCorner())
            jImage.getMask().getArray()[:] &= crosstalk  # Remove all other masks
            jImage -= bg

            iImage.scaledPlus(coeffs[ii, jj], jImage)

    # Set crosstalkStr bit only for those pixels that have been significantly modified
    # (not necessarily those that are bright originally).
    mask.clearMaskPlane(crosstalkPlane)

    mi -= subtrahend
